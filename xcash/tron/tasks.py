import time
from dataclasses import dataclass

import structlog
from celery import shared_task
from django.core.cache import cache
from django.db import transaction as db_transaction
from django.db.models import Q
from tron.client import TronClientError
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
from common.decorators import singleton_task
from common.time import ago

logger = structlog.get_logger()

TRON_BROADCAST_LOCK_TIMEOUT_SECONDS = 180
TRON_SENDER_BROADCAST_LOCK_TIMEOUT_SECONDS = TRON_BROADCAST_LOCK_TIMEOUT_SECONDS
TRON_RECEIPT_TX_TASK_TYPES = (TxTaskType.VaultSlotDeploy, TxTaskType.VaultSlotCollect)


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


def known_tx_hashes_for_task(task: TxTask) -> list[str]:
    """返回当前任务所有已知 tx_hash，按新版本优先查询。"""
    return [record.hash for record in known_tx_hash_records_for_task(task)]


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


def is_known_tron_hash_expired(record: KnownTronTxHash, *, now_ms: int) -> bool:
    return record.expires_at_ms is not None and now_ms >= int(record.expires_at_ms)


def find_tron_receipt_across_hashes(
    *,
    adapter,
    task: TxTask,
) -> tuple[TxCheckStatus | TxCheckResult | Exception, str | None]:
    """按所有历史 hash 查询 Tron 主动交易结果。

    Tron 过期重签会产生多个 txID；任一历史 hash 成功都足以把幂等 deploy/collect
    任务收口。只有所有已知 hash 均确定失败时才返回 FAILED；只要仍有 missing，
    就继续等待/后续重播，避免把仍可能上链的新 hash 过早判失败。
    """
    failed_result: TxCheckStatus | TxCheckResult | None = None
    failed_hash: str | None = None
    saw_missing = False
    now_ms = int(time.time() * 1000)

    for record in known_tx_hash_records_for_task(task):
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
        if not is_known_tron_hash_expired(record, now_ms=now_ms):
            saw_missing = True

    if failed_result is not None and not saw_missing:
        return failed_result, failed_hash
    return TxCheckStatus.MISSING, None


@shared_task(ignore_result=True)
@singleton_task(timeout=TRON_BROADCAST_LOCK_TIMEOUT_SECONDS, use_params=True)
def broadcast_tron_task(pk: int) -> None:
    tx_task = TronTxTask.objects.select_related("base_task", "chain", "sender").get(pk=pk)
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
        if tx_task.base_task.status == TxTaskStatus.QUEUED:
            if process_tron_receipt_task(tx_task.base_task):
                return
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


@shared_task(ignore_result=True)
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
    if not known_tx_hashes_for_task(task):
        return False
    adapter = AdapterFactory.get_adapter(task.chain.type)
    raw_result, matched_tx_hash = find_tron_receipt_across_hashes(
        adapter=adapter,
        task=task,
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
        updated = TxTask.mark_finalized_failed(
            task_id=task.pk,
            expected_status=task.status,
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


@shared_task(ignore_result=True)
@singleton_task(timeout=55)
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
        process_tron_receipt_task(task)


@shared_task(ignore_result=True)
@singleton_task(timeout=48, use_params=True)
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


@shared_task(ignore_result=True)
@singleton_task(timeout=64)
def scan_active_tron_chains() -> None:
    """每 2 秒巡检活跃 Tron 链，仅调度到期（now - last_scanned_at ≥ 扫描周期）的链。"""
    chains = (
        Chain.objects.filter(active=True, type=ChainType.TRON)
        .exclude(tron_api_key="")
    )
    for chain in chains:
        if chain.is_due_for_scan:
            scan_tron_chain.delay(chain.pk)
