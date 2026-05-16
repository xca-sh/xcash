from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from chains.models import BroadcastTask, BroadcastTaskFailureReason, Chain
from chains.service import ObservedTransferPayload, TransferService
from django.db import transaction as db_transaction
from django.utils import timezone
from evm.internal_tx.exceptions import UnknownInternalBroadcastError
from evm.internal_tx.handlers import get_handler
from evm.internal_tx.matchers import get_matcher
from web3 import Web3

logger = structlog.get_logger()


def _normalize_tx_hash(value: Any) -> str:
    raw = value.hex() if hasattr(value, "hex") else str(value)
    raw = raw.removeprefix("0x").lower()
    return f"0x{raw}"


def _normalize_address(value: Any) -> str:
    return Web3.to_checksum_address(str(value))


def _lookup_block_timestamp(*, chain: Chain, receipt: dict) -> tuple[int, datetime]:
    block_number = int(receipt["blockNumber"])
    block = chain.w3.eth.get_block(block_number)
    ts = int(block["timestamp"])
    occurred_at = datetime.fromtimestamp(ts, tz=timezone.get_current_timezone())
    return ts, occurred_at


def _block_hash_from_receipt(receipt: dict) -> str | None:
    raw = receipt.get("blockHash")
    if raw is None:
        return None
    return _normalize_tx_hash(raw)


def _receipt_status(receipt: dict) -> int:
    raw = receipt.get("status", 0)
    if isinstance(raw, str):
        return int(raw, 16) if raw.startswith("0x") else int(raw)
    return int(raw)


def _finalize_failed(
    *,
    broadcast_task: BroadcastTask,
    reason: BroadcastTaskFailureReason,
) -> None:
    with db_transaction.atomic():
        BroadcastTask.mark_finalized_failed(
            task_id=broadcast_task.pk,
            reason=reason,
            expected_stage=None,
        )
        handler = get_handler(broadcast_task.transfer_type)
        handler.finalize_failed(broadcast_task, reason)


def process_internal_transaction(
    *,
    chain: Chain,
    tx: dict,
    receipt: dict,
    block_timestamp: int | None = None,
    occurred_at: datetime | None = None,
) -> None:
    """处理 tx.from 已确认是系统地址的 EVM 交易。"""
    tx_hash = _normalize_tx_hash(tx["hash"])
    from_address = _normalize_address(tx["from"])

    broadcast_task = BroadcastTask.resolve_by_hash(chain=chain, tx_hash=tx_hash)
    if broadcast_task is None:
        raise UnknownInternalBroadcastError(
            chain_code=chain.code,
            tx_hash=tx_hash,
            from_address=from_address,
        )

    status = _receipt_status(receipt)
    if status == 0:
        _finalize_failed(
            broadcast_task=broadcast_task,
            reason=BroadcastTaskFailureReason.EXECUTION_REVERTED,
        )
        return

    matcher = get_matcher(broadcast_task.transfer_type)
    fact = matcher(chain=chain, broadcast_task=broadcast_task, receipt=receipt)
    if fact is None:
        _finalize_failed(
            broadcast_task=broadcast_task,
            reason=BroadcastTaskFailureReason.EXPECTED_TRANSFER_MISSING,
        )
        logger.warning(
            "EVM 内部交易 receipt 成功但 matcher 未找到预期 Transfer",
            chain=chain.code,
            tx_hash=tx_hash,
            transfer_type=broadcast_task.transfer_type,
            broadcast_task_id=broadcast_task.pk,
        )
        return

    block_number = int(receipt["blockNumber"])
    if block_timestamp is None or occurred_at is None:
        block_timestamp, occurred_at = _lookup_block_timestamp(
            chain=chain,
            receipt=receipt,
        )
    payload = ObservedTransferPayload(
        chain=chain,
        block=block_number,
        tx_hash=tx_hash,
        event_id=fact.event_id,
        from_address=fact.from_address,
        to_address=fact.to_address,
        crypto=fact.crypto,
        value=fact.value,
        amount=fact.amount,
        timestamp=block_timestamp,
        occurred_at=occurred_at,
        block_hash=_block_hash_from_receipt(receipt),
        source="evm-internal-tx",
    )
    TransferService.create_observed_transfer(observed=payload)
