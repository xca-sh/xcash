import structlog
from celery import shared_task
from django.db import transaction as db_transaction
from django.db.models import Q

from chains.models import Chain
from chains.models import ChainType
from chains.models import TxTaskStatus
from chains.tasks import dispatch_block_confirmation_checks_if_needed
from common.decorators import singleton_task
from common.time import ago
from evm.models import EvmTxTask
from evm.poller import EvmTaskPoller
from evm.scanner.rpc import EvmScannerRpcError
from evm.scanner.service import EvmScannerService

logger = structlog.get_logger()


@shared_task(ignore_result=True)
@singleton_task(timeout=30, use_params=True)
def _broadcast_evm_task(pk: int) -> None:
    # 任务入口统一使用 TxTask 命名，避免继续暴露旧的广播载荷概念。
    tx_task = EvmTxTask.objects.select_related("base_task").get(pk=pk)
    # 普通 Celery 入口只负责 QUEUED 首次广播；PENDING_CHAIN 重播统一由
    # poller 在超时与查 receipt 后触发，避免重复消息绕过重播间隔。
    if tx_task.base_task.status != TxTaskStatus.QUEUED:
        return
    if tx_task.has_lower_queued_nonce() or tx_task.is_pipeline_full():
        logger.info(
            "EVM 广播被阻断",
            task_pk=tx_task.pk,
            sender=tx_task.sender.address,
            chain=tx_task.chain.code,
            nonce=tx_task.nonce,
            reason=(
                "lower_queued_nonce"
                if tx_task.has_lower_queued_nonce()
                else "pipeline_full"
            ),
        )
        return
    tx_task.broadcast()
    # 广播成功后，链式调度同地址下一个 QUEUED nonce，快速填充 pipeline。
    tx_task.base_task.refresh_from_db(fields=["status"])
    if tx_task.base_task.status != TxTaskStatus.PENDING_CHAIN:
        return
    _chain_dispatch_next(tx_task)


def _chain_dispatch_next(completed_task: EvmTxTask) -> None:
    """广播成功后立即调度同发送地址下一个 QUEUED nonce，避免等待下一轮 dispatch 周期。"""
    if completed_task.is_pipeline_full():
        return
    next_task = (
        EvmTxTask.objects.select_related("base_task")
        .filter(
            sender=completed_task.sender,
            chain=completed_task.chain,
            base_task__status=TxTaskStatus.QUEUED,
        )
        .order_by("nonce")
        .first()
    )
    if next_task is not None:
        _broadcast_evm_task.delay(next_task.pk)


@shared_task(ignore_result=True)
@singleton_task(timeout=64)
@db_transaction.atomic
def dispatch_evm_tx_tasks() -> None:
    """定时调度 QUEUED 状态的 EVM 交易任务（Celery Beat 每 5 秒）。

    调度规则：
    - 每个 (sender, chain) 只放行最低 nonce 的任务，保证 nonce 按顺序进入 mempool
    - pipeline 未满（同发送地址 PENDING_CHAIN < EVM_PIPELINE_DEPTH）才放行
    - 4 分钟内已尝试过的不重复投递
    - 每轮最多投递 8 笔
    """
    tasks = (
        EvmTxTask.objects.select_for_update()
        .select_related("base_task")
        .filter(
            Q(last_attempt_at__isnull=True) | Q(last_attempt_at__lt=ago(minutes=4)),
            created_at__lt=ago(seconds=4),
            base_task__status=TxTaskStatus.QUEUED,
        )
        .order_by("sender_id", "nonce", "created_at")
    )

    selected: list[EvmTxTask] = []
    for task in tasks:
        if task.has_lower_queued_nonce():
            continue
        if task.is_pipeline_full():
            continue
        selected.append(task)
        if len(selected) >= 8:
            break

    for task in selected:
        task_pk = task.pk
        db_transaction.on_commit(lambda pk=task_pk: _broadcast_evm_task.delay(pk))


@shared_task(ignore_result=True)
@singleton_task(timeout=48, use_params=True)
def _scan_evm_chain(chain_pk: int) -> None:
    """按链执行一次 EVM VaultSlot 充值日志统一扫描。"""
    chain = Chain.objects.get(pk=chain_pk)
    previous_latest_block = chain.latest_block_number

    try:
        try:
            EvmScannerService.scan_chain(chain=chain)
        except EvmScannerRpcError:
            logger.warning("EVM 自扫描 RPC 失败", chain=chain.name)

        dispatch_block_confirmation_checks_if_needed(
            chain=chain,
            previous_latest_block=previous_latest_block,
        )
        EvmTaskPoller.poll_chain(chain=chain)
        logger.info("EVM 自扫描完成", chain=chain.name)
    finally:
        # 无论本轮是否命中 RPC 异常都推进 last_scanned_at，按固定周期重试，
        # 避免对不健康的节点每 2 秒一次连环重扫。
        chain.mark_scanned()


@shared_task(ignore_result=True)
def scan_active_evm_chains() -> None:
    """每 2 秒巡检活跃 EVM 链，仅调度到期（now - last_scanned_at ≥ 扫描周期）的链。"""
    for chain in Chain.objects.filter(active=True, type=ChainType.EVM):
        if chain.is_due_for_scan:
            _scan_evm_chain.delay(chain.pk)
