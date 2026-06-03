from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog
from web3.exceptions import TransactionNotFound

from chains.models import Chain
from chains.models import Transfer
from chains.models import TxTask
from common.internal_callback import send_internal_callback
from evm.models import VaultSlot

logger = structlog.get_logger()


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
    except Exception:  # noqa: BLE001
        tx = None
    return dict(receipt), dict(tx) if tx is not None else None


def _build_gas_payload(*, chain: Chain, tx_hash: str) -> dict[str, str | int]:
    receipt, tx = _load_receipt_and_tx(chain=chain, tx_hash=tx_hash)
    gas_used = _int_receipt_value(receipt.get("gasUsed"))
    gas_price = _receipt_gas_price(receipt, tx)
    gas_fee_wei = Decimal(gas_used) * Decimal(gas_price)
    native_crypto = chain.native_coin
    native_decimals = native_crypto.get_decimals(chain)
    gas_fee_native = gas_fee_wei.scaleb(-native_decimals)
    gas_fee_usdt = native_crypto.usd_amount(gas_fee_native)

    return {
        "tx_hash": tx_hash,
        "chain_code": chain.code,
        "native_crypto": native_crypto.symbol,
        "gas_used": gas_used,
        "gas_price": gas_price,
        "gas_fee_wei": _format_decimal(gas_fee_wei),
        "gas_fee_native": _format_decimal(gas_fee_native),
        "gas_fee_usdt": _format_decimal(gas_fee_usdt),
    }


def notify_vault_slot_deploy_gas_fee(*, tx_task: TxTask) -> None:
    """VaultSlot 部署确认后，通知 SaaS 对项目收取系统热钱包 gas 成本。"""
    if not tx_task.tx_hash:
        return
    try:
        slot = (
            VaultSlot.objects.select_related("project", "chain")
            .get(deploy_tx_task__base_task=tx_task)
        )
        gas_payload = _build_gas_payload(chain=slot.chain, tx_hash=tx_task.tx_hash)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "saas_gas_fee_callback_build_failed",
            operation="vault_slot_deploy",
            tx_task_id=tx_task.pk,
            tx_hash=tx_task.tx_hash,
            error=str(exc),
        )
        return

    payload = {
        **gas_payload,
        "operation": "vault_slot_deploy",
        "tx_task_id": tx_task.pk,
        "vault_slot_id": slot.pk,
        "vault_slot_address": slot.address,
        "vault_slot_usage": slot.usage,
    }
    send_internal_callback(
        event="gas_fee.vault_slot_deploy.confirmed",
        appid=slot.project.appid,
        sys_no=f"vault-slot-deploy:{tx_task.pk}",
        worth=payload["gas_fee_usdt"],
        currency="USDT",
        detail_payload=payload,
    )


def notify_vault_slot_collect_gas_fee(*, transfer: Transfer) -> None:
    """VaultSlot 归集确认后，通知 SaaS 对项目收取系统热钱包 gas 成本。"""
    try:
        slot = (
            VaultSlot.objects.select_related("project", "chain")
            .get(chain=transfer.chain, address__iexact=transfer.from_address)
        )
        gas_payload = _build_gas_payload(chain=transfer.chain, tx_hash=transfer.hash)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "saas_gas_fee_callback_build_failed",
            operation="vault_slot_collect",
            transfer_id=transfer.pk,
            tx_hash=transfer.hash,
            error=str(exc),
        )
        return

    payload = {
        **gas_payload,
        "operation": "vault_slot_collect",
        "transfer_id": transfer.pk,
        "vault_slot_id": slot.pk,
        "vault_slot_address": slot.address,
        "vault_slot_usage": slot.usage,
        "collected_crypto": transfer.crypto.symbol,
        "collected_amount": _format_decimal(transfer.amount),
    }
    send_internal_callback(
        event="gas_fee.vault_slot_collect.confirmed",
        appid=slot.project.appid,
        sys_no=f"vault-slot-collect:{transfer.pk}",
        worth=payload["gas_fee_usdt"],
        currency="USDT",
        detail_payload=payload,
    )
