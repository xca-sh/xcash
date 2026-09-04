import structlog
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.db import transaction as db_transaction
from django.db.models import Exists
from django.db.models import OuterRef
from django.db.models import Q

from chains.models import Chain
from chains.models import ChainType
from chains.models import TxTaskStatus
from chains.tasks import dispatch_block_confirmation_checks_if_needed
from common.decorators import PendingOnceTask
from common.decorators import singleton_task
from common.time import ago
from evm.models import EvmTxTask
from evm.poller import EvmTaskPoller
from evm.scanner.rpc import EvmScannerRpcError
from evm.scanner.service import EvmScannerService

logger = structlog.get_logger()

EVM_QUEUED_STUCK_ALERT_AFTER_MINUTES = 30
EVM_QUEUED_DISPATCH_ELIGIBLE_AFTER_SECONDS = 1


@shared_task(ignore_result=True, soft_time_limit=25, time_limit=30)
@singleton_task(timeout=35, use_params=True)
def _broadcast_evm_task(pk: int) -> None:
    # 任务入口统一使用 TxTask 命名，避免继续暴露旧的广播载荷概念。
    tx_task = EvmTxTask.objects.select_related("base_task").get(pk=pk)
    # 普通 Celery 入口只负责 QUEUED 首次广播；SUBMITTED 重播统一由
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
    if tx_task.base_task.status != TxTaskStatus.SUBMITTED:
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


@shared_task(
    base=PendingOnceTask,
    ignore_result=True,
    soft_time_limit=55,
    time_limit=60,
)
@singleton_task(timeout=64)
@db_transaction.atomic
def dispatch_evm_tx_tasks() -> None:
    """定时调度 QUEUED 状态的 EVM 交易任务。

    调度规则：
    - 每个 (sender, chain) 只放行最低 nonce 的任务，保证 nonce 按顺序进入 mempool
    - pipeline 未满（同发送地址 SUBMITTED < EVM_PIPELINE_DEPTH）才放行
    - 创建超过 1 秒的任务才放行，避开刚提交事务的短暂可见性抖动
    - 4 分钟内已尝试过的不重复投递
    - 每轮最多投递 8 笔

    队首必须先用 DISTINCT ON 收敛再做过滤：全系统每种链类型只有一个热钱包地址，
    每轮真正可能放行的就是每个 (sender, chain) 的最低 nonce 那一条，条数等于链数。
    若直接对全部 QUEUED 行做 select_for_update()+select_related()，PG 会把
    evm_evmtxtask 与 chains_txtask 两张表的命中行全部锁住，且循环内要逐行执行一次
    has_lower_queued_nonce() 的 EXISTS 查询。正常态队列只有个位数无感，但热钱包断
    gas / 队首卡死这类事故下 QUEUED 会按入账速率堆积，届时每 2 秒一轮的派发会锁住
    整个队列约一秒、并发出与积压量同阶的查询，而广播路径的 append_tx_hash 恰好需要
    同一批 TxTask 行锁——恢复期反被派发器自己拖住。
    """
    head_pks = list(
        EvmTxTask.objects.filter(base_task__status=TxTaskStatus.QUEUED)
        .order_by("sender_id", "chain_id", "nonce")
        .distinct("sender_id", "chain_id")
        .values_list("pk", flat=True)
    )
    tasks = (
        EvmTxTask.objects.select_for_update()
        .select_related("base_task")
        .filter(
            Q(last_attempt_at__isnull=True) | Q(last_attempt_at__lt=ago(minutes=4)),
            pk__in=head_pks,
            created_at__lt=ago(seconds=EVM_QUEUED_DISPATCH_ELIGIBLE_AFTER_SECONDS),
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


@shared_task(ignore_result=True, soft_time_limit=25, time_limit=30)
@singleton_task(timeout=35)
def scan_stuck_queued_evm_tx_tasks(limit: int = 32) -> int:
    """告警长期卡在 QUEUED 队首的 EVM 任务，避免 nonce 队列无声停摆。"""
    lower_queued = EvmTxTask.objects.filter(
        sender_id=OuterRef("sender_id"),
        chain_id=OuterRef("chain_id"),
        nonce__lt=OuterRef("nonce"),
        base_task__status=TxTaskStatus.QUEUED,
    )
    tasks = (
        EvmTxTask.objects.select_related("base_task", "sender", "chain")
        .annotate(has_lower_queued=Exists(lower_queued))
        .filter(
            base_task__status=TxTaskStatus.QUEUED,
            created_at__lt=ago(minutes=EVM_QUEUED_STUCK_ALERT_AFTER_MINUTES),
            has_lower_queued=False,
        )
        .order_by("created_at")[:limit]
    )
    alerted = 0
    for task in tasks:
        alerted += 1
        logger.warning(
            "EVM QUEUED 任务长期停留在 nonce 队首",
            evm_task_id=task.pk,
            tx_task_id=task.base_task_id,
            chain=task.chain.code,
            sender=task.sender.address,
            nonce=task.nonce,
            tx_type=task.base_task.tx_type,
            tx_hash=task.base_task.tx_hash,
            created_at=task.created_at,
            last_attempt_at=task.last_attempt_at,
        )
    return alerted


@shared_task(ignore_result=True, soft_time_limit=40, time_limit=50)
@singleton_task(timeout=55, use_params=True)
def _scan_evm_chain(chain_pk: int) -> None:
    """按链执行一次 EVM VaultSlot 充值日志统一扫描。"""
    chain = Chain.objects.get(pk=chain_pk)
    previous_latest_block = chain.latest_block_number

    try:
        try:
            EvmScannerService.scan_chain(chain=chain)
        except EvmScannerRpcError:
            logger.warning("EVM 自扫描 RPC 失败", chain=chain.name)
        except SoftTimeLimitExceeded:
            # 软超时要求立刻收尾，不再做后续动作；游标已按 chunk 分段提交，进度不丢。
            raise
        except Exception:  # noqa: BLE001
            # 扫描段的故障不能连带吞掉本轮确认调度：二者是相互独立的职责，
            # 此前共用一个 try 导致扫描一异常就整段跳过。
            logger.exception("EVM 自扫描失败", chain=chain.code)

        try:
            dispatch_block_confirmation_checks_if_needed(
                chain=chain,
                previous_latest_block=previous_latest_block,
            )
        except SoftTimeLimitExceeded:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("EVM 确认调度派发失败", chain=chain.code)
        logger.info("EVM 自扫描完成", chain=chain.name)
    finally:
        # 无论本轮是否命中 RPC 异常都推进 last_scanned_at，按固定周期重试，
        # 避免对不健康的节点每 2 秒一次连环重扫。
        chain.mark_scanned()


@shared_task(ignore_result=True, soft_time_limit=110, time_limit=120)
@singleton_task(timeout=125, use_params=True)
def poll_evm_chain_tx_tasks(chain_pk: int) -> None:
    """轮询单条 EVM 链在途主动交易的链上终局。

    从扫描任务里拆出来独立调度：此前它挂在扫描之后，扫描段一旦抛异常就整段跳过，
    部署/归集任务会永不终局，管线（EVM_PIPELINE_DEPTH）填满后该链全部主动交易停摆。
    Tron 侧一直有独立的 confirm_tron_receipt_tx_tasks，这里补齐对称能力。
    """
    chain = Chain.objects.get(pk=chain_pk)
    if not chain.active:
        return
    EvmTaskPoller.poll_chain(chain=chain)


@shared_task(base=PendingOnceTask, ignore_result=True)
@singleton_task(timeout=64)
def poll_active_evm_chains() -> None:
    """按固定周期为每条活跃 EVM 链派发一次在途交易终局轮询。

    互斥锁的作用同 scan_active_evm_chains：业务 worker 可以横向扩容
    （docker compose up -d --scale worker=N），多个实例并发跑这段巡检时，同一条链会被
    各自投递一次 poll_evm_chain_tx_tasks。重复消息不会重复执行（后者自带 per-chain 锁
    会跳过），但会白占 celery 队列，放大 broker 的内存压力。
    """
    for chain in Chain.objects.filter(active=True, type=ChainType.EVM):
        poll_evm_chain_tx_tasks.delay(chain.pk)


@shared_task(base=PendingOnceTask, ignore_result=True)
@singleton_task(timeout=64)
def scan_active_evm_chains() -> None:
    """每 2 秒巡检活跃 EVM 链，仅调度到期（now - last_scanned_at ≥ 扫描周期）的链。

    互斥锁与 tron.tasks.scan_active_tron_chains 对称：scan 队列可以横向扩容
    （docker compose up -d --scale worker-scan=N），多个实例并发跑这段巡检时，
    同一条到期链会被各自投递一次 _scan_evm_chain。重复消息不会重复执行（后者自带
    per-chain 锁会跳过），但会白占 scan 队列，放大 broker 的内存压力。
    """
    for chain in Chain.objects.filter(active=True, type=ChainType.EVM):
        if chain.is_due_for_scan:
            _scan_evm_chain.delay(chain.pk)
