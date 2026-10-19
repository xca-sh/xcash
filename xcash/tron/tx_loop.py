"""Tron 主动交易在 chain-runner 链循环中的两步：终局轮询与派发，以及二者共用的回执判定。

同一条链只有一个链线程在发交易（见 chains.runner），同一发送地址天然串行，不再需要
Celery 广播任务、发送地址锁、首播接力与 beat 周期补偿。新建任务由 TronTxTask.schedule
在事务内发 PG 通知，链线程被唤醒后立即补跑派发；派发永远按库里的任务状态工作。
"""

import time
from dataclasses import dataclass

import structlog
from django.db import transaction as db_transaction
from django.db.models import Q
from django.utils import timezone
from tron.client import TronClientError
from tron.client import TronHttpClient
from tron.models import TRON_MAX_BROADCAST_HASHES
from tron.models import TronTxTask
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
from chains.scanning import deadline_passed
from chains.vault_slot_balances import refresh_vault_slot_balance_for_collect_task
from chains.vault_slots import mark_deployed_by_task
from chains.vault_slots import mark_deployed_if_on_chain_for_task
from common.time import ago

logger = structlog.get_logger()

TRON_RECEIPT_TX_TASK_TYPES = (TxTaskType.VaultSlotDeploy, TxTaskType.VaultSlotCollect)
TRON_MISSING_TX_FINALITY_GRACE_MS = 5 * 60 * 1000
# 同一任务两次广播尝试的最小间隔，避免节点故障时每轮都对刚失败的任务空打一遍。
TRON_BROADCAST_RETRY_AFTER_MINUTES = 2
# 单任务两次回执查询的最小间隔。轮询步骤每轮（约 6s）都会跑，按 updated_at 过滤后单任务
# 仍是每 60s 查一次；mark_submitted 会刷新 updated_at，首查落在广播后约 60s，正好越过
# Tron 约 57s 的固化时间。
TRON_RECEIPT_RECHECK_SECONDS = 60


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
    """按回执收口本链在途的部署 / 归集。

    部署不产生用户资产入账，归集是 slot→vault（收款方为系统外 vault），二者都不会被扫描器
    当作「打入系统观察地址」的入账观测，无法靠扫描器确认；统一在此用 adapter.tx_result
    查回执推进终局，并在成功终局时按类型回调 SaaS 计费。
    """
    tasks = (
        TxTask.objects.select_related("chain", "sender")
        .prefetch_related("tx_hashes")
        .filter(
            chain=chain,
            tx_type__in=TRON_RECEIPT_TX_TASK_TYPES,
            status__in=(TxTaskStatus.QUEUED, TxTaskStatus.SUBMITTED),
            updated_at__lt=ago(seconds=TRON_RECEIPT_RECHECK_SECONDS),
        )
        .order_by("updated_at")
    )
    for index, task in enumerate(tasks):
        if index and deadline_passed(deadline):
            return
        try:
            process_tron_receipt_task(task)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Tron 主动交易回执收口失败", chain=chain.code, tx_task_id=task.pk
            )
        finally:
            # MISSING、确认数不足或 RPC 错误都要刷新 updated_at 轮转到队尾，否则预算不够
            # 时固定最老的几条会永久占住队首，后面的部署/归集永远得不到确认机会。
            TxTask.objects.filter(
                pk=task.pk,
                status__in=(TxTaskStatus.QUEUED, TxTaskStatus.SUBMITTED),
            ).update(updated_at=timezone.now())


def dispatch_tron_chain_round(chain: Chain, deadline: float) -> None:
    """派发本链待发的主动交易，并重签重播已过期的在途交易。

    同一条链只有一个链线程在发交易，同一发送地址自然串行——这是能量预检成立的前提：
    预检时储备能量不能被并发的另一笔交易消耗。Tron 没有 nonce 顺序约束，一笔失败不挡
    后面的任务；刚失败过的按 TRON_BROADCAST_RETRY_AFTER_MINUTES 退避。
    """
    now_ms = int(time.time() * 1000)
    tasks = (
        TronTxTask.objects.select_related("base_task", "sender")
        .filter(
            Q(base_task__status=TxTaskStatus.QUEUED)
            | Q(base_task__status=TxTaskStatus.SUBMITTED, expiration__lte=now_ms),
            broadcast_retry_due_q(),
            chain=chain,
        )
        .order_by("created_at")
    )
    for index, task in enumerate(tasks):
        if index and deadline_passed(deadline):
            return
        task.chain = chain
        broadcast_once(task)


def broadcast_once(tx_task: TronTxTask) -> None:
    """处理一笔待发任务：先按已知 hash 查回执，未上链才首播或重签重播。"""
    try:
        # 两条分支都必须先查链上回执再决定是否发交易。SUBMITTED 重播此前直接重签重播，
        # 而 Tron 的 expiration 只有 60s、固化却要约 57s，回执确认又有自己的节奏——交易
        # 往往已经上链、只是还没被确认看到，此时重播等于对同一笔业务再发一次：白烧能量，
        # 且 gas 成本会按重播产生的新 hash 上报、首笔漏报。
        if tx_task.base_task.status in (TxTaskStatus.QUEUED, TxTaskStatus.SUBMITTED):
            if process_tron_receipt_task(tx_task.base_task):
                return
            # 回执检查可能已推进状态（如失败终局但未收口成功），按最新状态决定后续动作。
            tx_task.base_task.refresh_from_db(fields=["status"])
        if tx_task.base_task.status == TxTaskStatus.QUEUED:
            tx_task.broadcast()
        elif tx_task.base_task.status == TxTaskStatus.SUBMITTED:
            tx_task.rebroadcast_expired_submitted()
    except TronClientError as exc:
        logger.warning(
            "Tron 任务广播失败",
            task_pk=tx_task.pk,
            chain=tx_task.chain.code,
            error=str(exc),
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Tron 任务广播异常", task_pk=tx_task.pk, chain=tx_task.chain.code
        )
