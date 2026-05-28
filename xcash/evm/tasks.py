import structlog
from celery import shared_task
from django.db import transaction as db_transaction
from django.db.models import Q

from chains.adapters import AdapterFactory
from chains.adapters import TxCheckResult
from chains.adapters import TxCheckStatus
from chains.models import Chain
from chains.models import ChainType
from chains.models import TxTask
from chains.models import TxTaskStage
from chains.models import TxTaskType
from evm.internal_tx.routing import NON_TRANSFER_TX_TASK_TYPES
from evm.internal_tx.routing import get_handler
from common.decorators import singleton_task
from common.time import ago
from evm.models import EvmTxTask
from evm.poller import EvmTaskPoller
from evm.scanner.rpc import EvmScannerRpcError
from evm.scanner.service import EvmScannerService

logger = structlog.get_logger()


def _tx_check_status(result: TxCheckStatus | TxCheckResult) -> TxCheckStatus:
    return result.status if isinstance(result, TxCheckResult) else result


def _has_required_confirmations(*, chain: Chain, result: TxCheckResult | None) -> bool:
    if result is None or result.block_number is None:
        return False
    confirmed_at_or_before = chain.latest_block_number - chain.confirm_block_count
    return int(result.block_number) <= confirmed_at_or_before


@shared_task(ignore_result=True)
@singleton_task(timeout=30, use_params=True)
def _broadcast_evm_task(pk: int) -> None:
    # 任务入口统一使用 TxTask 命名，避免继续暴露旧的广播载荷概念。
    tx_task = EvmTxTask.objects.select_related("base_task").get(pk=pk)
    # 普通 Celery 入口只负责 QUEUED 首次广播；PENDING_CHAIN 重播统一由
    # poller 在超时与查 receipt 后触发，避免重复消息绕过重播间隔。
    if (
        tx_task.base_task.success is not None
        or tx_task.base_task.stage != TxTaskStage.QUEUED
    ):
        return
    if tx_task.has_lower_queued_nonce() or tx_task.is_pipeline_full():
        logger.info(
            "EVM 广播被阻断",
            task_pk=tx_task.pk,
            address=tx_task.address.address,
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
    tx_task.base_task.refresh_from_db(fields=["stage", "success"])
    if (
        tx_task.base_task.stage != TxTaskStage.PENDING_CHAIN
        or tx_task.base_task.success is not None
    ):
        return
    _chain_dispatch_next(tx_task)


def _chain_dispatch_next(completed_task: EvmTxTask) -> None:
    """广播成功后立即调度同地址下一个 QUEUED nonce，避免等待下一轮 dispatch 周期。"""
    if completed_task.is_pipeline_full():
        return
    next_task = (
        EvmTxTask.objects.select_related("base_task")
        .filter(
            address=completed_task.address,
            chain=completed_task.chain,
            base_task__stage=TxTaskStage.QUEUED,
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
    - 每个 (address, chain) 只放行最低 nonce 的任务，保证 nonce 按顺序进入 mempool
    - pipeline 未满（同地址 PENDING_CHAIN < EVM_PIPELINE_DEPTH）才放行
    - 4 分钟内已尝试过的不重复投递
    - 每轮最多投递 8 笔
    """
    tasks = (
        EvmTxTask.objects.select_for_update()
        .select_related("base_task")
        .filter(
            Q(last_attempt_at__isnull=True) | Q(last_attempt_at__lt=ago(minutes=4)),
            created_at__lt=ago(seconds=4),
            base_task__stage=TxTaskStage.QUEUED,
        )
        .order_by("address_id", "nonce", "created_at")
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
@singleton_task(timeout=55)
def confirm_non_transfer_tx_tasks() -> None:
    """推进没有 Transfer 记录承载确认窗口的内部 EVM 任务。"""
    tasks = (
        TxTask.objects.select_related("chain")
        .filter(
            chain__type=ChainType.EVM,
            tx_type__in=NON_TRANSFER_TX_TASK_TYPES,
            stage=TxTaskStage.PENDING_CONFIRM,
            success__isnull=True,
            tx_hash__isnull=False,
        )
        .exclude(tx_hash="")
        .order_by("updated_at")[:32]
    )

    for task in tasks:
        adapter = AdapterFactory.get_adapter(task.chain.type)
        raw_result = adapter.tx_result(chain=task.chain, tx_hash=task.tx_hash)
        if isinstance(raw_result, Exception):
            logger.warning(
                "无 Transfer 内部交易确认查询失败",
                chain=task.chain.code,
                tx_task_id=task.pk,
                tx_hash=task.tx_hash,
                error=str(raw_result),
            )
            continue

        result_meta = raw_result if isinstance(raw_result, TxCheckResult) else None
        status = _tx_check_status(raw_result)
        if status == TxCheckStatus.CONFIRMED:
            if not _has_required_confirmations(chain=task.chain, result=result_meta):
                continue
            TxTask.mark_finalized_success(chain=task.chain, tx_hash=task.tx_hash)
        elif status == TxCheckStatus.CONFIRMING:
            continue
        elif status == TxCheckStatus.DROPPED:
            TxTask.reset_to_pending_chain(chain=task.chain, tx_hash=task.tx_hash)
        elif status == TxCheckStatus.FAILED:
            updated = TxTask.mark_finalized_failed(
                task_id=task.pk,
                expected_stage=TxTaskStage.PENDING_CONFIRM,
            )
            if updated:
                get_handler(TxTaskType(task.tx_type)).finalize_failed(task)


@shared_task(ignore_result=True)
@singleton_task(timeout=48, use_params=True)
def _scan_evm_chain(chain_pk: int) -> None:
    """按链执行一次 EVM VaultSlot 充值日志统一扫描。"""
    chain = Chain.objects.get(pk=chain_pk)

    try:
        EvmScannerService.scan_chain(chain=chain)
    except EvmScannerRpcError:
        logger.warning("EVM 自扫描 RPC 失败", chain=chain.name)

    EvmTaskPoller.poll_chain(chain=chain)
    logger.info("EVM 自扫描完成", chain=chain.name)


@shared_task(ignore_result=True)
def scan_active_evm_chains() -> None:
    """批量调度 EVM 链统一日志扫描任务。"""
    for chain_pk in Chain.objects.filter(
        active=True,
        type=ChainType.EVM,
    ).values_list("pk", flat=True):
        _scan_evm_chain.delay(chain_pk)
