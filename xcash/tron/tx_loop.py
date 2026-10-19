"""Tron 主动交易在 chain-runner 链循环中的两步：终局轮询与派发，以及二者共用的回执判定。

当前派发、广播与回执确认仍由 tron.tasks 中的 Celery 任务负责（复用这里的回执判定），
这两步暂为空实现，接管后再填入逻辑。
"""

import time
from dataclasses import dataclass

import structlog
from django.db import transaction as db_transaction
from django.db.models import Q
from tron.client import TronClientError
from tron.client import TronHttpClient
from tron.models import TRON_MAX_BROADCAST_HASHES
from tron.saas_gas_billing import notify_vault_slot_collect_gas_fee
from tron.saas_gas_billing import notify_vault_slot_deploy_gas_fee

from chains.adapters import TX_MISSING
from chains.adapters import AdapterFactory
from chains.adapters import TxCheckResult
from chains.adapters import TxCheckStatus
from chains.models import Chain
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import TxTaskType
from chains.vault_slot_balances import refresh_vault_slot_balance_for_collect_task
from chains.vault_slots import mark_deployed_by_task
from chains.vault_slots import mark_deployed_if_on_chain_for_task
from common.time import ago

logger = structlog.get_logger()

TRON_RECEIPT_TX_TASK_TYPES = (TxTaskType.VaultSlotDeploy, TxTaskType.VaultSlotCollect)
TRON_MISSING_TX_FINALITY_GRACE_MS = 5 * 60 * 1000
# 同一任务两次广播尝试的最小间隔；dispatch 补偿与首播接力共用，避免反复投递刚失败的任务。
TRON_BROADCAST_RETRY_AFTER_MINUTES = 2


@dataclass(frozen=True)
class KnownTronTxHash:
    hash: str
    expires_at_ms: int | None


def has_required_confirmations(*, chain: Chain, result: TxCheckResult) -> bool:
    if result.block_number is None:
        return False
    confirmed_at_or_before = chain.latest_block_number - chain.confirm_block_count
    return int(result.block_number) <= confirmed_at_or_before


def known_tx_hash_records_for_task(task: TxTask) -> list[KnownTronTxHash]:
    """返回当前任务所有已知 tx_hash 及该 hash 对应的过期时间。"""
    hashes: list[str] = []
    records: list[KnownTronTxHash] = []
    if task.tx_hash:
        hashes.append(task.tx_hash)
    for tx_hash in task.tx_hashes.order_by("-version"):
        if tx_hash.hash not in hashes:
            hashes.append(tx_hash.hash)
            records.append(
                KnownTronTxHash(
                    hash=tx_hash.hash,
                    expires_at_ms=tx_hash.expires_at_ms,
                )
            )
        elif task.tx_hash == tx_hash.hash and not records:
            records.append(
                KnownTronTxHash(
                    hash=tx_hash.hash,
                    expires_at_ms=tx_hash.expires_at_ms,
                )
            )
    if task.tx_hash and all(record.hash != task.tx_hash for record in records):
        records.insert(0, KnownTronTxHash(hash=task.tx_hash, expires_at_ms=None))
    return records


def solid_head_reached_timestamp(*, chain: Chain, timestamp_ms: int) -> bool:
    """仅当最新 solid block 的链上时间越过阈值才返回真，异常一律交给下轮重试。"""
    client = TronHttpClient(chain=chain)
    block_number = client.get_latest_solid_block_number()
    payload = client.get_solid_block_header(block_number=block_number)
    if not isinstance(payload, dict):
        raise TronClientError(f"invalid latest solid block from {chain.code}")
    try:
        raw_data = (payload.get("block_header") or {}).get("raw_data") or {}
        actual_block_number = int(raw_data.get("number") or 0)
        solid_timestamp_ms = int(raw_data.get("timestamp") or 0)
    except (AttributeError, TypeError, ValueError) as exc:
        raise TronClientError(f"invalid latest solid block from {chain.code}") from exc
    if actual_block_number != block_number or solid_timestamp_ms <= 0:
        raise TronClientError(f"invalid latest solid block from {chain.code}")
    return solid_timestamp_ms >= timestamp_ms


def find_tron_receipt_across_hashes(
    *,
    adapter,
    task: TxTask,
    records: tuple[KnownTronTxHash, ...],
) -> tuple[TxCheckResult | Exception, str | None]:
    """按所有历史 hash 查询 Tron 主动交易结果。

    Tron 过期重签会产生多个 txID；任一历史 hash 成功都足以把幂等 deploy/collect
    任务收口。全部 hash 明确失败时直接返回 FAILED；含 missing 时仅在达到重签
    上限且 expiration、本地宽限与 solid-head 链上时间均越过后返回 FAILED。
    """
    failed_result: TxCheckResult | None = None
    failed_hash: str | None = None
    missing_records: list[KnownTronTxHash] = []
    now_ms = int(time.time() * 1000)

    for record in records:
        raw_result = adapter.tx_result(chain=task.chain, tx_hash=record.hash)
        if isinstance(raw_result, Exception):
            return raw_result, None
        status = raw_result.status
        if status == TxCheckStatus.SUCCEEDED:
            return raw_result, record.hash
        if status == TxCheckStatus.FAILED:
            if failed_result is None:
                failed_result = raw_result
                failed_hash = record.hash
            continue
        missing_records.append(record)

    if failed_result is not None and not missing_records:
        return failed_result, failed_hash

    # 广播次数只是成本上限，不是失败证据。只有 capped 后所有仍 MISSING 的 hash
    # 都越过 expiration + 固化宽限，且最新 solid block 的链上时间也越过同一阈值，
    # 才能排除“截止前已打包、但当前 solid 节点暂不可见”的窗口并安全失败。
    if len(records) >= TRON_MAX_BROADCAST_HASHES and missing_records:
        deadlines = [
            int(record.expires_at_ms) + TRON_MISSING_TX_FINALITY_GRACE_MS
            for record in missing_records
            if record.expires_at_ms is not None
        ]
        if len(deadlines) == len(missing_records) and now_ms >= max(deadlines):
            try:
                solid_head_past_deadline = solid_head_reached_timestamp(
                    chain=task.chain,
                    timestamp_ms=max(deadlines),
                )
            except TronClientError as exc:
                return exc, None
            if solid_head_past_deadline:
                return (
                    failed_result or TxCheckResult(status=TxCheckStatus.FAILED),
                    failed_hash,
                )
    return TX_MISSING, None


def tron_hash_snapshot(task: TxTask) -> tuple[KnownTronTxHash, ...]:
    return tuple(known_tx_hash_records_for_task(task))


def broadcast_retry_due_q() -> Q:
    """从未尝试过、或距上次尝试已超过重试间隔的任务。"""
    return Q(last_attempt_at__isnull=True) | Q(
        last_attempt_at__lt=ago(minutes=TRON_BROADCAST_RETRY_AFTER_MINUTES)
    )


def notify_gas_fee_for_receipt_task(task: TxTask) -> None:
    """按任务类型把成功终局的链上成本回调给 SaaS 计费。"""
    if task.tx_type == TxTaskType.VaultSlotDeploy:
        notify_vault_slot_deploy_gas_fee(tx_task=task)
    elif task.tx_type == TxTaskType.VaultSlotCollect:
        notify_vault_slot_collect_gas_fee(tx_task=task)


def process_tron_receipt_task(task: TxTask) -> bool:
    """按已有 tx hash 推进单个 Tron 主动任务，返回是否已处理到链上事实。"""
    records = tron_hash_snapshot(task)
    if not records:
        return False
    adapter = AdapterFactory.get_adapter(task.chain.type)
    raw_result, matched_tx_hash = find_tron_receipt_across_hashes(
        adapter=adapter,
        task=task,
        records=records,
    )
    if isinstance(raw_result, Exception):
        logger.warning(
            "Tron 主动交易回执确认查询失败",
            chain=task.chain.code,
            tx_task_id=task.pk,
            error=str(raw_result),
        )
        return False

    status = raw_result.status
    if status == TxCheckStatus.SUCCEEDED:
        if matched_tx_hash is None:
            return False
        if task.status == TxTaskStatus.QUEUED:
            TxTask.mark_submitted(task_id=task.pk)
            task.status = TxTaskStatus.SUBMITTED
            task.tx_hash = matched_tx_hash
        if not has_required_confirmations(chain=task.chain, result=raw_result):
            return True
        updated = TxTask.mark_finalized_success(
            chain=task.chain,
            tx_hash=matched_tx_hash,
        )
        if updated:
            task.tx_hash = matched_tx_hash
            task.status = TxTaskStatus.SUCCEEDED
            if task.tx_type == TxTaskType.VaultSlotDeploy:
                mark_deployed_by_task(task)
            elif task.tx_type == TxTaskType.VaultSlotCollect:
                refresh_vault_slot_balance_for_collect_task(task)
            notify_gas_fee_for_receipt_task(task)
        return True

    if status == TxCheckStatus.MISSING:
        return False

    if status == TxCheckStatus.FAILED:
        # 查询 RPC 时不持有数据库锁；终局前锁住父任务并重核 hash 快照。
        # persist_signed_payload/append_tx_hash 复用同一行锁，因此并发新签名要么先
        # 进入快照、要么看到终局后停止广播，不会把刚追加的 hash 漏在失败判断之外。
        with db_transaction.atomic():
            # of=("self",) 限定只锁任务行：select_related 会把 chains_chain 与热钱包
            # chains_address join 进来，裸 FOR UPDATE 会把整条链和整个热钱包锁死，
            # 阻塞该链全部交易创建与入账落库。
            locked_task = (
                TxTask.objects.select_for_update(of=("self",))
                .select_related("chain", "sender")
                .get(pk=task.pk)
            )
            if tron_hash_snapshot(locked_task) != records:
                return False
            updated = TxTask.mark_finalized_failed(
                task_id=locked_task.pk,
                expected_status=locked_task.status,
            )
        if updated:
            task.status = TxTaskStatus.FAILED
            if task.tx_type == TxTaskType.VaultSlotDeploy:
                mark_deployed_if_on_chain_for_task(task)
            logger.warning(
                "Tron 主动交易失败终局",
                tx_task_id=task.pk,
                tx_type=task.tx_type,
                chain=task.chain.code,
                sender=task.sender.address,
                tx_hash=matched_tx_hash,
            )
        return bool(updated)

    return False


def poll_tron_chain_round(chain: Chain, deadline: float) -> None:
    """按回执收口本链在途的主动交易。"""


def dispatch_tron_chain_round(chain: Chain, deadline: float) -> None:
    """派发本链待发的主动交易，并重播已过期的在途交易。"""
