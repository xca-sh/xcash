from __future__ import annotations

import structlog
from django.db import transaction as db_transaction

from chains.models import Chain
from chains.models import TxTask
from chains.models import TxTaskType
from chains.models import VaultSlot
from chains.vault_slot_balances import refresh_vault_slot_balance_for_collect_task
from chains.vault_slots import get_backend
from chains.vault_slots import mark_deployed_by_task
from chains.vault_slots import mark_deployed_if_on_chain_for_task
from evm.hexutil import checksum_address
from evm.hexutil import parse_int
from evm.hexutil import required_hash
from evm.internal_tx.routing import UnknownInternalBroadcastError
from evm.internal_tx.routing import get_matcher
from evm.internal_tx.vault_slot_deploy import confirm_initial_native_balance_transfer
from evm.internal_tx.vault_slot_deploy import process_vault_slot_initial_native_balance
from evm.saas_gas_billing import notify_vault_slot_collect_gas_fee
from evm.saas_gas_billing import notify_vault_slot_deploy_gas_fee

logger = structlog.get_logger()


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


def _deploy_slot_is_on_chain(*, slot: VaultSlot, tx_task: TxTask) -> bool | None:
    """复核部署交易对应的 VaultSlot 是否已在链上有合约码。

    返回 True/False 表示链上有码/无码；返回 None 表示链上检查 RPC 异常、无法判定，
    调用方必须保持 SUBMITTED 等待下轮重试。
    """
    try:
        return get_backend(slot.chain).is_deployed_on_chain(
            chain=slot.chain,
            address=slot.address,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "EVM VaultSlot 部署成功收口前链上有码检查失败，保持待确认",
            chain=slot.chain.code,
            vault_slot_id=slot.pk,
            tx_task_id=tx_task.pk,
            error=str(exc),
        )
        return None


def _finalize_deploy_success(
    *,
    chain: Chain,
    tx_hash: str,
    tx_task: TxTask,
    receipt: dict,
) -> bool:
    slot = (
        VaultSlot.objects.select_related("chain").filter(deploy_tx_task=tx_task).first()
    )
    if slot is None:
        # 槽位已不存在（项目/客户/链级联删除，或 deploy_tx_task 已改指新任务）。
        # 这与"RPC 查不动"必须区分：重试永远不可能让槽位回来，若同样按"待确认"处理，
        # 任务会永久停在 SUBMITTED——poller 每轮重复拉 tx + receipt，并长期占住一个
        # EVM_PIPELINE_DEPTH 名额。既无 is_deployed 需要维护、也无项目可计费，
        # 直接成功终局收口，让管线名额和轮询开销都释放掉。
        logger.error(
            "EVM VaultSlot 部署收口找不到关联槽位，直接成功终局",
            chain=chain.code,
            tx_hash=tx_hash,
            tx_task_id=tx_task.pk,
        )
        TxTask.mark_finalized_success(chain=chain, tx_hash=tx_hash)
        return True

    # 部署交易 status=1 只代表调用未 revert，不代表槽位真的被部署：工厂地址本身
    # 无合约码时，deployVaultSlot 调用会"空成功"，槽位地址仍无码。若此时按成功
    # 收口会误标 is_deployed，之后 reconcile 见"有余额"反复重建归集任务空扫烧 gas。
    # 故先复核链上有码，确定无码则按失败收口，待工厂部署后重建部署任务恢复。
    is_deployed = _deploy_slot_is_on_chain(slot=slot, tx_task=tx_task)
    if is_deployed is None:
        return False
    if not is_deployed:
        logger.error(
            "EVM VaultSlot 部署交易成功但地址无合约码，按失败收口",
            chain=chain.code,
            tx_hash=tx_hash,
            tx_task_id=tx_task.pk,
        )
        _finalize_failed(tx_task=tx_task)
        return True

    transfer = None
    updated = False
    with db_transaction.atomic():
        transfer = process_vault_slot_initial_native_balance(
            chain=chain,
            tx_task=tx_task,
            tx_hash=tx_hash,
            receipt=receipt,
        )
        updated = TxTask.mark_finalized_success(chain=chain, tx_hash=tx_hash)
        if updated:
            tx_task.refresh_from_db()
            mark_deployed_by_task(tx_task)
        confirm_initial_native_balance_transfer(transfer)

    if updated:
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
    tx_hash = required_hash(tx["hash"])
    from_address = checksum_address(str(tx["from"]))

    tx_task = TxTask.resolve_by_hash(chain=chain, tx_hash=tx_hash)
    if tx_task is None:
        raise UnknownInternalBroadcastError(
            chain_code=chain.code,
            tx_hash=tx_hash,
            from_address=from_address,
        )

    status = parse_int(receipt.get("status", 0))
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
            receipt=receipt,
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
