from __future__ import annotations

from typing import Any

import structlog
from django.db import transaction as db_transaction
from web3 import Web3

from chains.models import Chain
from chains.models import TxTask
from chains.models import TxTaskType
from chains.vault_slot_balances import refresh_vault_slot_balance_for_collect_task
from chains.vault_slots import mark_deployed_by_task
from chains.vault_slots import mark_deployed_if_on_chain_for_task
from evm.internal_tx.routing import UnknownInternalBroadcastError
from evm.internal_tx.routing import get_matcher
from evm.saas_gas_billing import notify_vault_slot_collect_gas_fee
from evm.saas_gas_billing import notify_vault_slot_deploy_gas_fee

logger = structlog.get_logger()


def _normalize_tx_hash(value: Any) -> str:
    """转成带 0x 前缀的小写哈希。"""
    raw = value.hex() if hasattr(value, "hex") else str(value)
    raw = raw.removeprefix("0x").lower()
    return f"0x{raw}"


def _normalize_address(value: Any) -> str:
    """转 checksum 地址。"""
    return Web3.to_checksum_address(str(value))


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
            expected_status=None,
        )
        if not updated:
            return
        if tx_task.tx_type == TxTaskType.VaultSlotDeploy:
            mark_deployed_if_on_chain_for_task(tx_task)


def _finalize_deploy_success(*, chain: Chain, tx_hash: str, tx_task: TxTask) -> bool:
    updated = TxTask.mark_finalized_success(chain=chain, tx_hash=tx_hash)
    if updated:
        tx_task.refresh_from_db()
        mark_deployed_by_task(tx_task)
        notify_vault_slot_deploy_gas_fee(tx_task=tx_task)
    return True


def _finalize_collect_success(
    *,
    chain: Chain,
    tx_hash: str,
    tx_task: TxTask,
    tx: dict,
    receipt: dict,
) -> bool:
    matcher = get_matcher(TxTaskType.VaultSlotCollect)
    fact = matcher(chain=chain, tx_task=tx_task, receipt=receipt, tx=tx)
    if fact is None:
        logger.warning(
            "EVM VaultSlot 归集 receipt 成功但未找到预期资产移动事实，按空归集终局",
            chain=chain.code,
            tx_hash=tx_hash,
            tx_task_id=tx_task.pk,
        )
        updated = TxTask.mark_finalized_success(chain=chain, tx_hash=tx_hash)
        if updated:
            tx_task.refresh_from_db()
            notify_vault_slot_collect_gas_fee(tx_task=tx_task)
        return True

    updated = TxTask.mark_finalized_success(chain=chain, tx_hash=tx_hash)
    if updated:
        tx_task.refresh_from_db()
        refresh_vault_slot_balance_for_collect_task(tx_task)
        notify_vault_slot_collect_gas_fee(tx_task=tx_task)
    return True


def process_internal_transaction(
    *,
    chain: Chain,
    tx: dict,
    receipt: dict,
) -> bool:
    """处理 tx.from 已识别为系统地址的 EVM 交易。"""
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
        return True

    try:
        tx_type = TxTaskType(tx_task.tx_type)
    except ValueError:
        logger.warning(
            "EVM 内部交易 TxTask 类型未知，无法收口",
            chain=chain.code,
            tx_hash=tx_hash,
            tx_type=tx_task.tx_type,
            tx_task_id=tx_task.pk,
        )
        return False

    if tx_type == TxTaskType.VaultSlotDeploy:
        return _finalize_deploy_success(
            chain=chain,
            tx_hash=tx_hash,
            tx_task=tx_task,
        )
    if tx_type == TxTaskType.VaultSlotCollect:
        return _finalize_collect_success(
            chain=chain,
            tx_hash=tx_hash,
            tx_task=tx_task,
            tx=tx,
            receipt=receipt,
        )

    logger.warning(
        "EVM 内部交易缺少成功收口逻辑",
        chain=chain.code,
        tx_hash=tx_hash,
        tx_type=tx_task.tx_type,
        tx_task_id=tx_task.pk,
    )
    return False
