from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import structlog
from django.conf import settings
from procrastinate.contrib.django import app
from web3.exceptions import TransactionNotFound

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


@dataclass(frozen=True, kw_only=True)
class GasTxDetail:
    """gas_fee 回调的 tx_detail：成本 + 可复算的链上输入，不掺业务元数据。

    复算关系：gas_cost = gas_used × gas_price / 10^decimals × native_price。
    金额一律用去尾零字符串，避免 JSON 浮点精度问题。
    """

    gas_cost: str
    tx_hash: str
    chain: str
    gas_used: int
    gas_price: int
    native_price: str


def _int_receipt_value(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    return int(value)


def _receipt_gas_price(receipt: dict, tx: dict | None = None) -> int:
    for key in ("effectiveGasPrice", "gasPrice"):
        if key in receipt:
            price = _int_receipt_value(receipt.get(key))
            if price > 0:
                return price
    if tx is not None and "gasPrice" in tx:
        return _int_receipt_value(tx.get("gasPrice"))
    return 0


def _format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".")


def _load_receipt_and_tx(*, chain: Chain, tx_hash: str) -> tuple[dict, dict | None]:
    receipt = chain.w3.eth.get_transaction_receipt(tx_hash)  # noqa: SLF001
    if receipt is None:
        raise TransactionNotFound(tx_hash)
    try:
        tx = chain.w3.eth.get_transaction(tx_hash)  # noqa: SLF001
    except Exception:  # noqa
        tx = None
    return dict(receipt), dict(tx) if tx is not None else None  # noqa


def _build_tx_detail(*, chain: Chain, tx_hash: str) -> GasTxDetail:
    """从回执算出 gas 成本（USDT），连同对账输入打包成 GasTxDetail。"""
    receipt, tx = _load_receipt_and_tx(chain=chain, tx_hash=tx_hash)
    gas_used = _int_receipt_value(receipt.get("gasUsed"))
    gas_price = _receipt_gas_price(receipt, tx)
    native_crypto = chain.native_coin
    native_decimals = native_crypto.get_decimals(chain)
    native_price = native_crypto.usd_amount(Decimal(1))  # 无价源时降级为 0
    gas_fee_native = (Decimal(gas_used) * Decimal(gas_price)).scaleb(-native_decimals)
    gas_cost = gas_fee_native * native_price

    return GasTxDetail(
        gas_cost=_format_decimal(gas_cost),
        tx_hash=tx_hash,
        chain=ChainCode(chain.code).label,
        gas_used=gas_used,
        gas_price=gas_price,
        native_price=_format_decimal(native_price),
    )


def send_vault_slot_deploy_gas_fee(*, tx_task: TxTask) -> None:
    slot = VaultSlot.objects.select_related("project", "chain", "customer").get(
        deploy_tx_task=tx_task
    )
    tx_detail = _build_tx_detail(chain=slot.chain, tx_hash=tx_task.tx_hash)
    send_saas_callback(
        SaasCallback(
            event=CallbackEvent.GAS_FEE_VAULT_SLOT_DEPLOY,
            appid=slot.project.appid,
            sys_no=f"vault-slot-deploy:{tx_task.pk}",
            currency="USDT",
            tx_detail=asdict(tx_detail),
            uid=slot.customer.uid if slot.customer_id else None,
        )
    )


def send_vault_slot_collect_gas_fee(*, tx_task: TxTask) -> None:
    schedule = VaultSlotCollectSchedule.objects.select_related(
        "vault_slot__project",
        "vault_slot__customer",
        "chain",
    ).get(tx_task=tx_task)
    slot = schedule.vault_slot
    tx_detail = _build_tx_detail(chain=schedule.chain, tx_hash=tx_task.tx_hash)
    send_saas_callback(
        SaasCallback(
            event=CallbackEvent.GAS_FEE_VAULT_SLOT_COLLECT,
            appid=slot.project.appid,
            sys_no=f"vault-slot-collect:{tx_task.pk}",
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


@app.task(name="evm.report_vault_slot_deploy_gas_fee", retry=GAS_FEE_REPORT_RETRY)
def report_vault_slot_deploy_gas_fee(*, tx_task_id: int) -> None:
    """按部署交易的 receipt 算出 gas 成本，入队 SaaS 计费回调。"""
    tx_task = TxTask.objects.get(pk=tx_task_id)
    try:
        send_vault_slot_deploy_gas_fee(tx_task=tx_task)
    except Exception as exc:
        logger.warning(
            "saas_gas_fee_report_failed",
            operation="vault_slot_deploy",
            tx_task_id=tx_task_id,
            tx_hash=tx_task.tx_hash,
            error=str(exc),
        )
        raise


@app.task(name="evm.report_vault_slot_collect_gas_fee", retry=GAS_FEE_REPORT_RETRY)
def report_vault_slot_collect_gas_fee(*, tx_task_id: int) -> None:
    """按归集交易的 receipt 算出 gas 成本，入队 SaaS 计费回调。"""
    tx_task = TxTask.objects.get(pk=tx_task_id)
    try:
        send_vault_slot_collect_gas_fee(tx_task=tx_task)
    except Exception as exc:
        logger.warning(
            "saas_gas_fee_report_failed",
            operation="vault_slot_collect",
            tx_task_id=tx_task_id,
            tx_hash=tx_task.tx_hash,
            error=str(exc),
        )
        raise
