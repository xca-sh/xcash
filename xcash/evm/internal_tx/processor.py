from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from django.db import transaction as db_transaction
from django.utils import timezone
from web3 import Web3

from chains.models import Chain
from chains.models import TxTask
from chains.models import TxTaskType
from chains.service import ObservedTransferCreateResult
from chains.service import ObservedTransferPayload
from chains.service import TransferService
from evm.internal_tx.routing import NON_TRANSFER_TX_TASK_TYPES
from evm.internal_tx.routing import UnknownInternalBroadcastError
from evm.internal_tx.routing import get_handler
from evm.internal_tx.routing import get_matcher

logger = structlog.get_logger()


def _normalize_tx_hash(value: Any) -> str:
    """转成带 0x 前缀的小写哈希。"""
    raw = value.hex() if hasattr(value, "hex") else str(value)
    raw = raw.removeprefix("0x").lower()
    return f"0x{raw}"


def _normalize_address(value: Any) -> str:
    """转 checksum 地址。"""
    return Web3.to_checksum_address(str(value))


def _lookup_block_timestamp(*, chain: Chain, receipt: dict) -> tuple[int, datetime]:
    """补查 receipt 所在块的时间戳与本地化时间。"""
    block_number = int(receipt["blockNumber"])
    block = chain.w3.eth.get_block(block_number)
    ts = int(block["timestamp"])
    occurred_at = datetime.fromtimestamp(ts, tz=timezone.get_current_timezone())
    return ts, occurred_at


def _block_hash_from_receipt(receipt: dict) -> str:
    """从 receipt 提取必填 block_hash。"""
    raw = receipt["blockHash"]
    return _normalize_tx_hash(raw)


def _receipt_status(receipt: dict) -> int:
    """读取 receipt.status，兼容 int / 十进制串 / 0x 十六进制串。"""
    raw = receipt.get("status", 0)
    if isinstance(raw, str):
        return int(raw, 16) if raw.startswith("0x") else int(raw)
    return int(raw)


def _finalize_failed(*, tx_task: TxTask) -> None:
    """把 TxTask 标记为最终失败，并触发对应业务的失败收尾。"""
    with db_transaction.atomic():
        updated = TxTask.mark_finalized_failed(
            task_id=tx_task.pk,
            expected_stage=None,
        )
        if not updated:
            return
        try:
            handler = get_handler(TxTaskType(tx_task.tx_type))
        except (KeyError, ValueError):
            return
        handler.finalize_failed(tx_task)


def process_internal_transaction(
    *,
    chain: Chain,
    tx: dict,
    receipt: dict,
    block_timestamp: int | None = None,
    occurred_at: datetime | None = None,
) -> ObservedTransferCreateResult | None:
    """处理 tx.from 已确认是系统地址的 EVM 交易。"""
    tx_hash = _normalize_tx_hash(tx["hash"])
    from_address = _normalize_address(tx["from"])

    tx_task = TxTask.resolve_by_hash(chain=chain, tx_hash=tx_hash)
    if tx_task is None:
        raise UnknownInternalBroadcastError(
            chain_code=chain.code,
            tx_hash=tx_hash,
            from_address=from_address,
        )

    status = _receipt_status(receipt)
    if status == 0:
        _finalize_failed(tx_task=tx_task)
        return None

    if tx_task.tx_type in NON_TRANSFER_TX_TASK_TYPES:
        TxTask.mark_pending_confirm(chain=chain, tx_hash=tx_hash)
        return None

    matcher = get_matcher(TxTaskType(tx_task.tx_type))
    fact = matcher(chain=chain, tx_task=tx_task, receipt=receipt, tx=tx)
    if fact is None:
        logger.warning(
            "EVM 内部交易 receipt 成功但 matcher 未找到预期 Transfer",
            chain=chain.code,
            tx_hash=tx_hash,
            tx_type=tx_task.tx_type,
            tx_task_id=tx_task.pk,
        )
        return None

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
    return TransferService.create_observed_transfer(observed=payload)
