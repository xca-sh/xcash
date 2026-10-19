import time

import structlog
from celery import shared_task
from django.core.cache import cache
from django.db import transaction as db_transaction
from django.db.models import Q
from django.utils import timezone
from tron.client import TronClientError
from tron.models import TronTxTask
from tron.tx_loop import TRON_RECEIPT_TX_TASK_TYPES
from tron.tx_loop import broadcast_retry_due_q
from tron.tx_loop import process_tron_receipt_task

from chains.constants import ChainType
from chains.models import TxTask
from chains.models import TxTaskStatus
from common.decorators import singleton_task
from common.time import ago

logger = structlog.get_logger()

TRON_BROADCAST_LOCK_TIMEOUT_SECONDS = 180
TRON_SENDER_BROADCAST_LOCK_TIMEOUT_SECONDS = TRON_BROADCAST_LOCK_TIMEOUT_SECONDS


def sender_broadcast_lock_key(*, chain_id: int, sender_id: int) -> str:
    return f"tron:broadcast:chain:{chain_id}:sender:{sender_id}"


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
    first_broadcast = False
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
            first_broadcast = True
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

    # 接力必须放在释放发送地址锁之后：先投递再删锁，下一笔可能在删锁前被 worker 取走、
    # 撞锁跳过，接力就断了。只在首播成功（QUEUED→SUBMITTED）后接力，与 EVM 一致；
    # 失败的不接力，交给 dispatch 按尝试间隔补偿，避免节点故障时连环空打。
    if first_broadcast:
        tx_task.base_task.refresh_from_db(fields=["status"])
        if tx_task.base_task.status == TxTaskStatus.SUBMITTED:
            chain_dispatch_next_queued(tx_task)


def chain_dispatch_next_queued(completed_task: TronTxTask) -> None:
    """首播成功后立即投递同发送地址最早的下一笔 QUEUED 任务，不等 dispatch 周期。

    同批建出的任务在 schedule 提交时各自投递，但发送地址锁只放行一笔，其余撞锁跳过；
    靠这里逐笔接力，积压才能按广播速度连续排空。沿用 dispatch 的尝试间隔过滤：
    刚失败过的任务留给 dispatch 退避重试，不让它恒占接力队首、挡住后面的任务。
    """
    next_pk = (
        TronTxTask.objects.filter(
            broadcast_retry_due_q(),
            chain_id=completed_task.chain_id,
            sender_id=completed_task.sender_id,
            base_task__status=TxTaskStatus.QUEUED,
        )
        .order_by("created_at")
        .values_list("pk", flat=True)
        .first()
    )
    if next_pk is not None:
        broadcast_tron_task.delay(next_pk)


@shared_task(
    ignore_result=True,
    soft_time_limit=55,
    time_limit=60,
)
@singleton_task(timeout=64)
@db_transaction.atomic
def dispatch_tron_tx_tasks() -> None:
    """周期补偿：重试首播失败或接力断开的 QUEUED 任务，并重播已过期的 SUBMITTED 任务。

    首次广播由 TronTxTask.schedule 在提交后直接投递、并由 chain_dispatch_next_queued
    逐笔接力；本任务每轮只放行最早一笔，同发送地址同时只有一笔能拿到广播锁。
    """
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
            broadcast_retry_due_q(),
            created_at__lt=ago(seconds=2),
        )
        .order_by("created_at")[:1]
    )
    for task in tasks:
        db_transaction.on_commit(lambda pk=task.pk: broadcast_tron_task.delay(pk))


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
