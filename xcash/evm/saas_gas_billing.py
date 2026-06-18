from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import structlog
from celery import shared_task
from django.db import transaction
from web3.exceptions import TransactionNotFound

from chains.constants import ChainCode
from chains.models import Chain
from chains.models import TxTask
from chains.models import VaultSlot
from chains.models import VaultSlotCollectSchedule
from common.saas_callback import CallbackEvent
from common.saas_callback import SaasCallback
from common.saas_callback import send_saas_callback

logger = structlog.get_logger()

GAS_FEE_CALLBACK_RETRY_BACKOFF_SECONDS = 60


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


def notify_vault_slot_deploy_gas_fee(*, tx_task: TxTask) -> None:
    """VaultSlot 部署确认后，通知 SaaS 对项目收取系统热钱包 gas 成本。"""
    if not tx_task.tx_hash:
        return
    try:
        send_vault_slot_deploy_gas_fee(tx_task=tx_task)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "saas_gas_fee_callback_build_failed",
            operation="vault_slot_deploy",
            tx_task_id=tx_task.pk,
            tx_hash=tx_task.tx_hash,
            error=str(exc),
        )
        transaction.on_commit(
            lambda tx_task_id=tx_task.pk: retry_vault_slot_deploy_gas_fee.delay(
                tx_task_id
            )
        )
        return


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


def notify_vault_slot_collect_gas_fee(*, tx_task: TxTask) -> None:
    """VaultSlot 归集确认后，通知 SaaS 对项目收取系统热钱包 gas 成本。"""
    if not tx_task.tx_hash:
        return
    try:
        send_vault_slot_collect_gas_fee(tx_task=tx_task)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "saas_gas_fee_callback_build_failed",
            operation="vault_slot_collect",
            tx_task_id=tx_task.pk,
            tx_hash=tx_task.tx_hash,
            error=str(exc),
        )
        transaction.on_commit(
            lambda tx_task_id=tx_task.pk: retry_vault_slot_collect_gas_fee.delay(
                tx_task_id
            )
        )
        return


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


@shared_task(bind=True, ignore_result=True, max_retries=20)
def retry_vault_slot_deploy_gas_fee(self, tx_task_id: int) -> None:
    tx_task = TxTask.objects.get(pk=tx_task_id)
    try:
        send_vault_slot_deploy_gas_fee(tx_task=tx_task)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "saas_gas_fee_callback_retry_failed",
            operation="vault_slot_deploy",
            tx_task_id=tx_task_id,
            error=str(exc),
            retry=self.request.retries,  # noqa
        )
        raise self.retry(  # noqa
            exc=exc,
            countdown=GAS_FEE_CALLBACK_RETRY_BACKOFF_SECONDS
            * (2**self.request.retries),  # noqa
        ) from exc


@shared_task(bind=True, ignore_result=True, max_retries=20)
def retry_vault_slot_collect_gas_fee(self, tx_task_id: int) -> None:
    tx_task = TxTask.objects.get(pk=tx_task_id)
    try:
        send_vault_slot_collect_gas_fee(tx_task=tx_task)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "saas_gas_fee_callback_retry_failed",
            operation="vault_slot_collect",
            tx_task_id=tx_task_id,
            error=str(exc),
            retry=self.request.retries,  # noqa
        )
        raise self.retry(  # noqa
            exc=exc,
            countdown=GAS_FEE_CALLBACK_RETRY_BACKOFF_SECONDS
            * (2**self.request.retries),  # noqa
        ) from exc
