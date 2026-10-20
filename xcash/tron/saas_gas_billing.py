from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from decimal import Decimal

import structlog
from django.conf import settings
from procrastinate.contrib.django import app
from tron.client import TronClientError
from tron.client import TronHttpClient

from chains.constants import ChainCode
from chains.models import Chain
from chains.models import TxTask
from chains.models import VaultSlot
from chains.models import VaultSlotCollectSchedule
from common.jobs import BackoffTable
from common.jobs import defer_once
from common.saas_callback import SAAS_CALLBACK_RETRY_DELAYS
from common.saas_callback import CallbackEvent
from common.saas_callback import SaasCallback
from common.saas_callback import send_saas_callback

logger = structlog.get_logger()

# 计费上报任务：先查 receipt 算出成本，再入队 SaaS 回调；任一步失败按回调同一张退避表
# 重试（最长每小时一次，总窗口约 15 小时）。SaaS 以 sys_no 去重，重跑只扣一次。
GAS_FEE_REPORT_RETRY = BackoffTable(SAAS_CALLBACK_RETRY_DELAYS)
TRON_ENERGY_MARKET_PRICE_ENERGY = Decimal("65000")
TRON_ENERGY_MARKET_PRICE_TRX = Decimal("3")


@dataclass(frozen=True, kw_only=True)
class TronTxDetail:
    gas_cost: str
    tx_hash: str
    chain: str
    fee_sun: int
    energy_usage_total: int
    net_usage: int
    native_price: str


def format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".")


def int_payload_value(payload: dict, key: str) -> int:
    try:
        return int(payload.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def build_tx_detail(*, chain: Chain, tx_hash: str) -> TronTxDetail:
    payload = TronHttpClient(chain=chain).get_transaction_info_by_id(tx_hash)
    # 必须确认回执确实属于这笔交易再计价。gettransactioninfobyid 走 walletsolidity，
    # 负载均衡到固化头略落后的后端时会返回空对象——那样 fee 与 energy 都会被当成 0，
    # 成本按 0 上报；而 sys_no 在 SaaS 侧幂等去重，这笔成本此后永远无法补正。
    # 抛异常交给上层 notify_* 的退避重试处理，等固化头跟上即可拿到真实回执。
    if not isinstance(payload, dict) or str(payload.get("id") or "") != tx_hash:
        raise TronClientError(
            f"transaction info not found for {tx_hash} from {chain.code}"
        )
    fee_sun = int_payload_value(payload, "fee")
    receipt = payload.get("receipt") or {}
    if not isinstance(receipt, dict):
        receipt = {}
    energy_usage_total = int_payload_value(receipt, "energy_usage_total")
    native_crypto = chain.native_coin
    native_decimals = native_crypto.get_decimals(chain)
    native_price = native_crypto.usd_amount(Decimal(1))
    burned_trx = Decimal(fee_sun).scaleb(-native_decimals)
    energy_cost_trx = (
        Decimal(energy_usage_total)
        * TRON_ENERGY_MARKET_PRICE_TRX
        / TRON_ENERGY_MARKET_PRICE_ENERGY
    )
    gas_cost = (burned_trx + energy_cost_trx) * native_price
    return TronTxDetail(
        gas_cost=format_decimal(gas_cost),
        tx_hash=tx_hash,
        chain=ChainCode(chain.code).label,
        fee_sun=fee_sun,
        energy_usage_total=energy_usage_total,
        net_usage=int_payload_value(receipt, "net_usage"),
        native_price=format_decimal(native_price),
    )


def send_vault_slot_deploy_gas_fee(*, tx_task: TxTask) -> None:
    slot = VaultSlot.objects.select_related("project", "chain", "customer").get(
        deploy_tx_task=tx_task
    )
    tx_detail = build_tx_detail(chain=slot.chain, tx_hash=tx_task.tx_hash)
    send_saas_callback(
        SaasCallback(
            event=CallbackEvent.GAS_FEE_VAULT_SLOT_DEPLOY,
            appid=slot.project.appid,
            sys_no=f"tron-vault-slot-deploy:{tx_task.pk}",
            currency="USDT",
            tx_detail=asdict(tx_detail),
            uid=slot.customer.uid if slot.customer_id else None,
        )
    )


def send_vault_slot_collect_gas_fee(*, tx_task: TxTask) -> None:
    """归集任务确认终局后回调 SaaS 计费。

    归集交易的资金接收方是系统外的商户 vault，不会被扫描器当作入账观测；
    因此 slot/project 经归集计划（VaultSlotCollectSchedule.tx_task）反查，
    而非依赖归集 Transfer。sys_no 以 tx_task.pk 收敛，保证一次归集任务恰好计费一次。
    """
    schedule = VaultSlotCollectSchedule.objects.select_related(
        "vault_slot__project",
        "vault_slot__customer",
        "chain",
    ).get(tx_task=tx_task)
    slot = schedule.vault_slot
    tx_detail = build_tx_detail(chain=schedule.chain, tx_hash=tx_task.tx_hash)
    send_saas_callback(
        SaasCallback(
            event=CallbackEvent.GAS_FEE_VAULT_SLOT_COLLECT,
            appid=slot.project.appid,
            sys_no=f"tron-vault-slot-collect:{tx_task.pk}",
            currency="USDT",
            tx_detail=asdict(tx_detail),
            uid=slot.customer.uid if slot.customer_id else None,
        )
    )


def enqueue_vault_slot_deploy_gas_fee_report(*, tx_task: TxTask) -> None:
    """在部署成功终局的事务内入队 gas 计费上报，与终局状态推进同一事务提交。

    终局推进与上报分开提交时，两者之间崩溃会让 SaaS 永久漏计费；同事务后要么都没发生、
    下一轮轮询重新终局，要么上报任务已在队列里。未对接 SaaS 时不入队。
    """
    if not settings.IS_SAAS or not tx_task.tx_hash:
        return
    defer_once(report_vault_slot_deploy_gas_fee, tx_task_id=tx_task.pk)


def enqueue_vault_slot_collect_gas_fee_report(*, tx_task: TxTask) -> None:
    """在归集成功终局的事务内入队 gas 计费上报（语义同部署）。"""
    if not settings.IS_SAAS or not tx_task.tx_hash:
        return
    defer_once(report_vault_slot_collect_gas_fee, tx_task_id=tx_task.pk)


@app.task(name="tron.report_vault_slot_deploy_gas_fee", retry=GAS_FEE_REPORT_RETRY)
def report_vault_slot_deploy_gas_fee(*, tx_task_id: int) -> None:
    """按部署交易的 receipt 算出 gas 成本，入队 SaaS 计费回调。"""
    tx_task = TxTask.objects.get(pk=tx_task_id)
    try:
        send_vault_slot_deploy_gas_fee(tx_task=tx_task)
    except Exception as exc:
        logger.warning(
            "tron_saas_gas_fee_report_failed",
            operation="vault_slot_deploy",
            tx_task_id=tx_task_id,
            tx_hash=tx_task.tx_hash,
            error=str(exc),
        )
        raise


@app.task(name="tron.report_vault_slot_collect_gas_fee", retry=GAS_FEE_REPORT_RETRY)
def report_vault_slot_collect_gas_fee(*, tx_task_id: int) -> None:
    """按归集交易的 receipt 算出 gas 成本，入队 SaaS 计费回调。"""
    tx_task = TxTask.objects.get(pk=tx_task_id)
    try:
        send_vault_slot_collect_gas_fee(tx_task=tx_task)
    except Exception as exc:
        logger.warning(
            "tron_saas_gas_fee_report_failed",
            operation="vault_slot_collect",
            tx_task_id=tx_task_id,
            tx_hash=tx_task.tx_hash,
            error=str(exc),
        )
        raise
