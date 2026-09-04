import time
from dataclasses import dataclass

import structlog
from celery import shared_task
from django.core.cache import cache
from django.db import transaction as db_transaction
from django.db.models import Q
from django.utils import timezone
from tron.client import TronClientError
from tron.client import TronHttpClient
from tron.models import TRON_MAX_BROADCAST_HASHES
from tron.models import TronTxTask
from tron.saas_gas_billing import notify_vault_slot_collect_gas_fee
from tron.saas_gas_billing import notify_vault_slot_deploy_gas_fee
from tron.scanner import TronScanner

from chains.adapters import AdapterFactory
from chains.adapters import TxCheckResult
from chains.adapters import TxCheckStatus
from chains.constants import ChainType
from chains.models import Chain
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import TxTaskType
from chains.vault_slot_balances import refresh_vault_slot_balance_for_collect_task
from chains.vault_slots import mark_deployed_by_task
from chains.vault_slots import mark_deployed_if_on_chain_for_task
from common.decorators import PendingOnceTask
from common.decorators import singleton_task
from common.time import ago

logger = structlog.get_logger()

TRON_BROADCAST_LOCK_TIMEOUT_SECONDS = 180
TRON_SENDER_BROADCAST_LOCK_TIMEOUT_SECONDS = TRON_BROADCAST_LOCK_TIMEOUT_SECONDS
TRON_RECEIPT_TX_TASK_TYPES = (TxTaskType.VaultSlotDeploy, TxTaskType.VaultSlotCollect)
TRON_MISSING_TX_FINALITY_GRACE_MS = 5 * 60 * 1000


@dataclass(frozen=True)
class KnownTronTxHash:
    hash: str
    expires_at_ms: int | None


def tx_check_status(result: TxCheckStatus | TxCheckResult) -> TxCheckStatus:
    return result.status if isinstance(result, TxCheckResult) else result


def has_required_confirmations(*, chain: Chain, result: TxCheckResult | None) -> bool:
    if result is None or result.block_number is None:
        return False
    confirmed_at_or_before = chain.latest_block_number - chain.confirm_block_count
    return int(result.block_number) <= confirmed_at_or_before


def sender_broadcast_lock_key(*, chain_id: int, sender_id: int) -> str:
    return f"tron:broadcast:chain:{chain_id}:sender:{sender_id}"


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
    payload = client.get_solid_block(block_number=block_number)
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
) -> tuple[TxCheckStatus | TxCheckResult | Exception, str | None]:
    """按所有历史 hash 查询 Tron 主动交易结果。

    Tron 过期重签会产生多个 txID；任一历史 hash 成功都足以把幂等 deploy/collect
    任务收口。全部 hash 明确失败时直接返回 FAILED；含 missing 时仅在达到重签
    上限且 expiration、本地宽限与 solid-head 链上时间均越过后返回 FAILED。
    """
    failed_result: TxCheckStatus | TxCheckResult | None = None
    failed_hash: str | None = None
    missing_records: list[KnownTronTxHash] = []
    now_ms = int(time.time() * 1000)

    for record in records:
        raw_result = adapter.tx_result(chain=task.chain, tx_hash=record.hash)
        if isinstance(raw_result, Exception):
            return raw_result, None
        status = tx_check_status(raw_result)
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
                return failed_result or TxCheckStatus.FAILED, failed_hash
    return TxCheckStatus.MISSING, None


def tron_hash_snapshot(task: TxTask) -> tuple[KnownTronTxHash, ...]:
    return tuple(known_tx_hash_records_for_task(task))


# 广播的时间预算必须显式声明：Celery 全局默认是 30s 软 / 60s 硬，远小于本任务
# 180s 的互斥锁存活时间。用默认值时任务会在 60s 被硬杀（不走 finally），锁只能等
# TTL 过期，同一笔交易在这段时间内无法再广播。
@shared_task(ignore_result=True, soft_time_limit=150, time_limit=170)
@singleton_task(timeout=TRON_BROADCAST_LOCK_TIMEOUT_SECONDS, use_params=True)
def broadcast_tron_task(pk: int) -> None:
    tx_task = TronTxTask.objects.select_related("base_task", "chain", "sender").get(
        pk=pk
    )
    lock_key = sender_broadcast_lock_key(
        chain_id=tx_task.chain_id,
        sender_id=tx_task.sender_id,
    )
    acquired = cache.add(
        lock_key,
        "true",
        TRON_SENDER_BROADCAST_LOCK_TIMEOUT_SECONDS,
    )
    if not acquired:
        logger.info(
            "Tron 任务广播跳过，同一发送地址已有任务执行中",
            task_pk=tx_task.pk,
            chain=tx_task.chain.code,
            sender=tx_task.sender.address,
        )
        return
    try:
        # 两条分支都必须先查链上回执再决定是否发交易。SUBMITTED 重播此前直接重签重播，
        # 而 Tron 的 expiration 只有 60s、固化却要约 57s，confirm_tron_receipt_tx_tasks
        # 每轮只取 32 条、按 updated_at 排序会把刚提交的任务排到队尾——在途任务一多，
        # 交易往往已经上链、只是还没被确认任务看到，此时重播等于对同一笔业务再发一次：
        # 白烧能量，且 gas 成本会按重播产生的新 hash 上报、首笔漏报。
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
    finally:
        cache.delete(lock_key)


@shared_task(
    base=PendingOnceTask,
    ignore_result=True,
    soft_time_limit=55,
    time_limit=60,
)
@singleton_task(timeout=64)
@db_transaction.atomic
def dispatch_tron_tx_tasks() -> None:
    now_ms = int(time.time() * 1000)
    tasks = (
        TronTxTask.objects.select_for_update()
        .select_related("base_task")
        .filter(
            Q(base_task__status=TxTaskStatus.QUEUED)
            | Q(
                base_task__status=TxTaskStatus.SUBMITTED,
                expiration__lte=now_ms,
            ),
            Q(last_attempt_at__isnull=True) | Q(last_attempt_at__lt=ago(minutes=2)),
            created_at__lt=ago(seconds=2),
        )
        .order_by("created_at")[:1]
    )
    for task in tasks:
        db_transaction.on_commit(lambda pk=task.pk: broadcast_tron_task.delay(pk))


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

    result_meta = raw_result if isinstance(raw_result, TxCheckResult) else None
    status = tx_check_status(raw_result)
    if status == TxCheckStatus.SUCCEEDED:
        if matched_tx_hash is None:
            return False
        if task.status == TxTaskStatus.QUEUED:
            TxTask.mark_submitted(task_id=task.pk)
            task.status = TxTaskStatus.SUBMITTED
            task.tx_hash = matched_tx_hash
        if not has_required_confirmations(chain=task.chain, result=result_meta):
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


@shared_task(ignore_result=True, soft_time_limit=45, time_limit=55)
@singleton_task(timeout=58)
def confirm_tron_receipt_tx_tasks() -> None:
    """按回执收口 Tron 主动发起的链上任务(部署 / 归集)。

    部署不产生用户资产入账,归集是 slot→vault(收款方为系统外 vault),二者都不会被
    扫描器当作「打入系统观察地址」的入账观测,无法靠扫描器确认;统一在此用
    adapter.tx_result 查回执推进终局,并在成功终局时按类型回调 SaaS 计费。
    """
    tasks = (
        TxTask.objects.select_related("chain", "sender")
        .prefetch_related("tx_hashes")
        .filter(
            chain__type=ChainType.TRON,
            tx_type__in=TRON_RECEIPT_TX_TASK_TYPES,
            status__in=(TxTaskStatus.QUEUED, TxTaskStatus.SUBMITTED),
        )
        .order_by("updated_at")[:32]
    )
    for task in tasks:
        try:
            process_tron_receipt_task(task)
        finally:
            # MISSING、确认数不足或 RPC 错误都要轮转到队尾，否则固定最老 32 条
            # 会永久占住批次，使后续部署/归集任务永远得不到确认机会。
            TxTask.objects.filter(
                pk=task.pk,
                status__in=(TxTaskStatus.QUEUED, TxTaskStatus.SUBMITTED),
            ).update(updated_at=timezone.now())


@shared_task(ignore_result=True, soft_time_limit=40, time_limit=50)
@singleton_task(timeout=55, use_params=True)
def scan_tron_chain(chain_pk: int) -> None:
    chain = Chain.objects.get(pk=chain_pk)
    if not chain.active:
        return
    if chain.type == ChainType.TRON and not chain.tron_api_key:
        logger.warning("Tron 资产扫描跳过，缺少 API Key", chain=chain.code)
        return

    try:
        try:
            summary = TronScanner.scan_chain(chain=chain)
        except TronClientError:
            logger.warning("Tron 资产扫描 RPC 失败", chain=chain.code)
            return

        logger.info(
            "Tron 资产扫描完成",
            chain=chain.code,
            filter_addresses=summary.filter_addresses,
            blocks_scanned=summary.blocks_scanned,
            events_seen=summary.events_seen,
        )
    finally:
        # 无论成功还是 RPC 失败都推进 last_scanned_at，按固定周期重试。
        chain.mark_scanned()


@shared_task(base=PendingOnceTask, ignore_result=True)
@singleton_task(timeout=64)
def scan_active_tron_chains() -> None:
    """每 2 秒巡检活跃 Tron 链，仅调度到期（now - last_scanned_at ≥ 扫描周期）的链。"""
    chains = Chain.objects.filter(active=True, type=ChainType.TRON).exclude(
        tron_api_key=""
    )
    for chain in chains:
        if chain.is_due_for_scan:
            scan_tron_chain.delay(chain.pk)
