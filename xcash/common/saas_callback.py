from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog
from django.conf import settings
from django.db import models
from django.utils import timezone
from procrastinate.contrib.django import app

from common import http_client
from common.jobs import PRIORITY_URGENT
from common.jobs import BackoffTable
from common.jobs import defer_once

logger = structlog.get_logger()

# SaaS 侧接收回调的固定路径；SAAS_CALLBACK_URL 只配 scheme+host，路径由这里拼
_SAAS_CALLBACK_PATH = "/callbacks/xcash"

# 退避表（秒）：第 n 次失败后等待 SAAS_CALLBACK_RETRY_DELAYS[n] 再试，共重试 20 次。
# 覆盖窗口：前 5 次共约 46 分钟，之后每小时一次，总计约 15 小时。
# SaaS 侧以 sys_no 去重，重复送达只扣一次。gas 计费上报任务沿用同一张表。
SAAS_CALLBACK_RETRY_DELAYS = (8, 60, 300, 600, 1800) + (3600,) * 15


class CallbackEvent(models.TextChoices):
    """xcash → SaaS 回调的事件枚举，限定 event 可选值。

    命名空间即业务大类（invoice.* / deposit.* / gas_fee.* / aml.*），SaaS 据此路由。
    """

    INVOICE_CONFIRMED = "invoice.confirmed", "Invoice 确认"
    DEPOSIT_CONFIRMED = "deposit.confirmed", "Deposit 确认"
    GAS_FEE_VAULT_SLOT_DEPLOY = (
        "gas_fee.vault_slot_deploy.confirmed",
        "Gas 费：VaultSlot 部署",
    )
    GAS_FEE_VAULT_SLOT_COLLECT = (
        "gas_fee.vault_slot_collect.confirmed",
        "Gas 费：VaultSlot 归集",
    )
    # 一次成功的外部 AML 查询；引擎只报次数，单次价格由 SaaS 按套餐核算
    AML_SCREENED = "aml.screened", "AML 查询成功"


@dataclass(frozen=True, kw_only=True)
class SaasCallback:
    """xcash → SaaS 回调的统一数据结构（契约的单一定义处）。

    业务大类由 event 命名空间表达（invoice.* / deposit.* / gas_fee.* / aml.*），SaaS 按 event 路由。
    金额字段按 event 大类严格匹配（由 __post_init__ 强约束，杜绝缺失、多给或与 event 不匹配）：
    - invoice/deposit → 只带 worth（成交/充值等值金额）；
    - gas_fee → 只带 tx_detail（链上成本明细，含 gas_cost）；
    - aml → 两者都不带：只报一次查询，单价由 SaaS 按套餐决定。
    sys_no 是 SaaS 侧扣费的幂等键。uid 仅在 VaultSlot 能关联 Customer 时携带，
    供 SaaS 共享充值项目定位实际用户。
    """

    event: CallbackEvent
    appid: str
    sys_no: str
    currency: str = ""
    worth: str | None = None
    tx_detail: dict | None = None
    uid: str | None = None

    def __post_init__(self) -> None:
        CallbackEvent(self.event)  # 限定 event 取值，非法值抛 ValueError
        family = str(self.event).split(".", 1)[0]
        if family == "gas_fee":
            if self.tx_detail is None or self.worth is not None:
                raise ValueError("gas_fee 回调必须且只能带 tx_detail")
        elif family == "aml":
            if self.tx_detail is not None or self.worth is not None:
                raise ValueError("aml 回调不带 worth / tx_detail")
        elif self.worth is None or self.tx_detail is not None:
            raise ValueError(f"{self.event} 回调必须且只能带 worth")

    def to_payload(self) -> dict:
        """序列化为发往 SaaS 的 JSON body；按约束只会落入一个金额字段。"""
        payload: dict = {
            "event": str(self.event),
            "appid": self.appid,
            "sys_no": self.sys_no,
            "timestamp": timezone.now().isoformat(),
        }
        if self.currency:
            payload["currency"] = self.currency
        if self.worth is not None:
            payload["worth"] = self.worth
        if self.tx_detail is not None:
            payload["tx_detail"] = self.tx_detail
        if self.uid is not None:
            payload["uid"] = self.uid
        return payload


def send_saas_callback(callback: SaasCallback) -> None:
    """在调用方当前事务内入队 SaaS 回调，随业务事务一起提交或回滚。

    IS_SAAS=False 视为未对接 SaaS，直接跳过（没 token 也过不了 SaaS 的鉴权）。
    不加 queueing_lock：sys_no 由 SaaS 去重，同一事件不会因并发入队而重复扣费。
    """
    if not settings.IS_SAAS:
        return

    defer_once(
        deliver_saas_callback,
        priority=PRIORITY_URGENT,
        payload=callback.to_payload(),
    )


@app.task(
    name="common.deliver_saas_callback",
    retry=BackoffTable(SAAS_CALLBACK_RETRY_DELAYS, retry_exceptions=(httpx.HTTPError,)),
)
def deliver_saas_callback(*, payload: dict) -> None:
    """向 SaaS 发送回调 POST 请求；HTTP 失败抛出，由退避表安排重试。

    入参是已序列化好的 payload（SaasCallback.to_payload()），保持 JSON 可序列化。
    """
    if not settings.IS_SAAS:
        return
    url = f"{settings.SAAS_CALLBACK_URL.rstrip('/')}{_SAAS_CALLBACK_PATH}"

    try:
        resp = http_client.post(
            url,
            timeout=10,
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.SAAS_API_TOKEN}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(
            "saas_callback_failed",
            url=url,
            callback_event=payload.get("event"),
            appid=payload.get("appid"),
            sys_no=payload.get("sys_no"),
            error=str(exc),
        )
        # DEBUG 环境不做退避重试，只通知一次
        if settings.DEBUG:
            return
        raise
