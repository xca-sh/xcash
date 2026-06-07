from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog
from celery import shared_task
from django.conf import settings
from django.db import models
from django.db import transaction
from django.utils import timezone

logger = structlog.get_logger()

# SaaS 侧接收回调的固定路径；SAAS_CALLBACK_URL 只配 scheme+host，路径由这里拼
_SAAS_CALLBACK_PATH = "/callbacks/xcash"

# 指数退避序列（秒）：第 N 次重试前等待 _RETRY_BACKOFF[N]，超出长度使用最后一个值
# 覆盖窗口：前 5 次共 ~46 分钟，之后每小时一次，配合 max_retries=20 总计约 15 小时
_RETRY_BACKOFF = (8, 60, 300, 600, 1800, 3600)


class CallbackEvent(models.TextChoices):
    """xcash → SaaS 回调的事件枚举，限定 event 可选值。

    命名空间即业务大类（invoice.* / deposit.* / gas_fee.*），SaaS 据此路由。
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


@dataclass(frozen=True, kw_only=True)
class SaasCallback:
    """xcash → SaaS 回调的统一数据结构（契约的单一定义处）。

    业务大类由 event 命名空间表达（invoice.* / deposit.* / gas_fee.*），SaaS 按 event 路由。
    金额按 event 大类二选一、且必有其一（由 __post_init__ 强约束，杜绝
    「两者都为 None / 都给 / 与 event 不匹配」）：
    - invoice/deposit → worth（成交/充值等值金额）；
    - gas_fee → tx_detail（链上成本明细，含 gas_cost）。
    """

    event: CallbackEvent
    appid: str
    sys_no: str
    currency: str
    worth: str | None = None
    tx_detail: dict | None = None

    def __post_init__(self) -> None:
        CallbackEvent(self.event)  # 限定 event 取值，非法值抛 ValueError
        if str(self.event).startswith("gas_fee."):
            if self.tx_detail is None or self.worth is not None:
                raise ValueError("gas_fee 回调必须且只能带 tx_detail")
        elif self.worth is None or self.tx_detail is not None:
            raise ValueError(f"{self.event} 回调必须且只能带 worth")

    def to_payload(self) -> dict:
        """序列化为发往 SaaS 的 JSON body；按约束只会落入一个金额字段。"""
        payload: dict = {
            "event": str(self.event),
            "appid": self.appid,
            "sys_no": self.sys_no,
            "currency": self.currency,
            "timestamp": timezone.now().isoformat(),
        }
        if self.worth is not None:
            payload["worth"] = self.worth
        if self.tx_detail is not None:
            payload["tx_detail"] = self.tx_detail
        return payload


def _retry_countdown(retries: int) -> int:
    return _RETRY_BACKOFF[min(retries, len(_RETRY_BACKOFF) - 1)]


def send_saas_callback(callback: SaasCallback) -> None:
    """
    在事务提交后异步发送回调给 SaaS。
    IS_SAAS=False 视为未对接 SaaS，直接跳过（没 token 也过不了 SaaS 的鉴权）。
    """
    if not settings.IS_SAAS:
        return

    transaction.on_commit(
        lambda: _deliver_saas_callback.delay(payload=callback.to_payload())
    )


@shared_task(
    bind=True,
    ignore_result=True,
    max_retries=20,
    soft_time_limit=10,
    time_limit=15,
    acks_late=True,
    reject_on_worker_lost=True,
)
def _deliver_saas_callback(self, *, payload: dict) -> None:
    """Celery task：向 SaaS 发送回调 POST 请求。

    入参是已序列化好的 payload（SaasCallback.to_payload()），保持 JSON 可序列化，
    兼容 broker 里的在途消息。
    """
    if not settings.IS_SAAS:
        return
    url = f"{settings.SAAS_CALLBACK_URL.rstrip('/')}{_SAAS_CALLBACK_PATH}"

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                url,
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
            retry=self.request.retries,  # noqa
        )
        # DEBUG 环境不做指数退避重试，只通知一次
        if settings.DEBUG:
            return
        self.retry(countdown=_retry_countdown(self.request.retries), exc=exc)  # noqa
