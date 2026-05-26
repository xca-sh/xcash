import structlog
from celery import shared_task
from django.core.cache import cache
from django.db import transaction as db_transaction
from django.db.models import Q

from chains.models import Chain
from chains.models import ChainType
from chains.models import TxTask
from chains.models import TxTaskStage
from common.decorators import singleton_task
from common.time import ago
from evm.models import EvmTxTask
from evm.poller import EvmTaskPoller
from evm.scanner.rpc import EvmScannerRpcError
from evm.scanner.service import EvmChainScannerService

logger = structlog.get_logger()

# Ethereum L1 的出块间隔约 12 秒，作为 avg_block_interval 查询失败时的保守回退。
_DEFAULT_AVG_BLOCK_INTERVAL_SECONDS = 15
# 采样最近 20 个块计算平均间隔，兼顾精度与一次兜底任务的 RPC 开销。
_AVG_BLOCK_INTERVAL_SAMPLE_SIZE = 20
# 平均间隔按链缓存 5 分钟，不必每轮都查 RPC。
_AVG_BLOCK_INTERVAL_CACHE_TTL_SECONDS = 300
# 单轮重扫最多处理的候选任务数，避免长时间积压把重扫拖成长任务。
_RESCAN_CANDIDATE_LIMIT = 50


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
def dispatch_due_evm_tx_tasks() -> None:
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
@singleton_task(timeout=48, use_params=True)
def _scan_evm_chain(chain_pk: int) -> None:
    """按链执行一次 EVM DepositSlot 充值日志统一扫描。"""
    chain = Chain.objects.get(pk=chain_pk)

    result = None
    try:
        result = EvmChainScannerService.scan_chain(chain=chain)
    except EvmScannerRpcError:
        logger.warning("EVM 自扫描 RPC 失败", chain=chain.code)

    EvmTaskPoller.poll_chain(chain=chain)
    if result is None:
        return

    logger.info(
        "EVM 自扫描完成",
        chain=chain.code,
        native_from=result.native.from_block,
        native_to=result.native.to_block,
        native_logs=result.native.observed_logs,
        native_created=result.native.created_transfers,
        erc20_from=result.erc20.from_block,
        erc20_to=result.erc20.to_block,
        erc20_logs=result.erc20.observed_logs,
        erc20_created=result.erc20.created_transfers,
    )


@shared_task(ignore_result=True)
def scan_active_evm_chains() -> None:
    """批量调度 EVM 链统一日志扫描任务。"""
    for chain_pk in Chain.objects.filter(
        active=True,
        type=ChainType.EVM,
    ).values_list("pk", flat=True):
        _scan_evm_chain.delay(chain_pk)


def _estimate_avg_block_interval(chain) -> float:
    """估算链的平均出块间隔秒数，查询失败或异常回退到保守默认值。

    - 采样最近 _AVG_BLOCK_INTERVAL_SAMPLE_SIZE 个块，取首尾 timestamp 差除以块间距，
      避免累计 N 次 get_block 的网络开销。
    - 任何 RPC / 解析异常都回退到 _DEFAULT_AVG_BLOCK_INTERVAL_SECONDS，兜底任务以
      "能跑" 为最高优先级。
    """
    try:
        latest_block_number = int(chain.w3.eth.block_number)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "EVM 兜底任务获取最新区块失败，使用默认平均出块间隔",
            chain=chain.code,
            error=str(exc),
        )
        return float(_DEFAULT_AVG_BLOCK_INTERVAL_SECONDS)

    sample_span = _AVG_BLOCK_INTERVAL_SAMPLE_SIZE
    start_block_number = latest_block_number - sample_span
    if start_block_number <= 0:
        return float(_DEFAULT_AVG_BLOCK_INTERVAL_SECONDS)

    try:
        latest_block = chain.get_block_with_poa_retry(latest_block_number)
        start_block = chain.get_block_with_poa_retry(start_block_number)
        latest_ts = int(latest_block["timestamp"])
        start_ts = int(start_block["timestamp"])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "EVM 兜底任务采样块间隔失败，使用默认平均出块间隔",
            chain=chain.code,
            error=str(exc),
        )
        return float(_DEFAULT_AVG_BLOCK_INTERVAL_SECONDS)

    if latest_ts <= start_ts:
        return float(_DEFAULT_AVG_BLOCK_INTERVAL_SECONDS)
    return (latest_ts - start_ts) / sample_span


def _get_avg_block_interval(chain) -> float:
    """按链缓存平均出块间隔，减少反复采样对 RPC 的压力。"""
    cache_key = f"evm:avg_block_interval:{chain.pk}"
    cached = cache.get(cache_key)
    if cached is not None:
        return float(cached)
    interval = _estimate_avg_block_interval(chain)
    cache.set(cache_key, interval, _AVG_BLOCK_INTERVAL_CACHE_TTL_SECONDS)
    return interval


def _compute_rescan_threshold_seconds(chain) -> tuple[int, float]:
    """按链动态推算 "超出该时长仍停在 PENDING_CHAIN 即视为需要兜底" 的阈值。

    = max(30, avg_block_interval * confirm_block_count * 2 + 30)
    +30s 给 mempool → 首块打包留一段缓冲。阈值下限 30s 防止极短链把兜底刷成热点。
    返回 (阈值秒, 用于日志的平均出块间隔)。
    """
    avg_interval = _get_avg_block_interval(chain)
    raw = avg_interval * max(chain.confirm_block_count, 1) * 2 + 30
    return max(30, int(raw)), avg_interval


def _collect_blocks_from_receipts(chain, task: TxTask) -> set[int]:
    """遍历某个 TxTask 的所有历史 tx_hash 查询 receipt，返回命中成功的块号集合。

    - 所有 hash 都未上链 (TransactionNotFound / None) → 返回空集合，留给下一轮再试。
    - 单个 hash 查询抛 RPC 异常 → 记日志 + continue，避免一条坏哈希卡住整个兜底。
    - status=0 (链上 revert) 在兜底范围之外，本轮不把它当作 "命中"，由专门的失败终局路径处理。
    """
    from web3.exceptions import TransactionNotFound

    hashes: set[str] = set()
    for history_hash in task.tx_hashes.values_list("hash", flat=True):
        if history_hash:
            hashes.add(history_hash)
    if task.tx_hash:
        hashes.add(task.tx_hash)

    blocks: set[int] = set()
    for tx_hash in hashes:
        try:
            receipt = chain.w3.eth.get_transaction_receipt(tx_hash)  # noqa: SLF001
        except TransactionNotFound:
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "EVM 兜底查询 receipt 异常",
                chain=chain.code,
                task_pk=task.pk,
                tx_hash=tx_hash,
                error=str(exc),
            )
            continue

        if not receipt:
            continue
        status = receipt.get("status")
        block_number = receipt.get("blockNumber")
        if status != 1 or block_number is None:
            continue
        blocks.add(int(block_number))
    return blocks


@shared_task(ignore_result=True)
@singleton_task(timeout=48, use_params=True)
def _rescan_pending_evm_chain(chain_pk: int) -> None:
    """针对单条 EVM 链，给长时间停在 PENDING_CHAIN 的任务做 receipt 重扫兜底。

    若扫描器因 RPC 故障、节点重组、长时间下线等原因漏扫了某些块，对应 TxTask 会
    卡在 PENDING_CHAIN。这里主动对候选任务的全部历史 tx_hash 查链上 receipt，
    命中后交给扫描器 rescan_blocks 对那些块做一次定点重扫，让 Transfer +
    process/confirm 管线自然推进。
    """
    from chains.models import Chain

    chain = Chain.objects.get(pk=chain_pk)
    if not chain.active:
        return

    threshold_seconds, avg_block_interval = _compute_rescan_threshold_seconds(chain)
    stale_tasks = list(
        TxTask.objects.filter(
            chain_id=chain_pk,
            stage=TxTaskStage.PENDING_CHAIN,
            success__isnull=True,
            updated_at__lt=ago(seconds=threshold_seconds),
        ).order_by("updated_at")[:_RESCAN_CANDIDATE_LIMIT]
    )

    blocks_to_rescan: set[int] = set()
    resolved_count = 0
    for task in stale_tasks:
        task_blocks = _collect_blocks_from_receipts(chain, task)
        if task_blocks:
            resolved_count += 1
            blocks_to_rescan.update(task_blocks)

    if blocks_to_rescan:
        EvmChainScannerService.rescan_blocks(
            chain=chain, block_numbers=blocks_to_rescan
        )

    logger.info(
        "EVM 重扫完成",
        chain=chain.code,
        stale_count=len(stale_tasks),
        resolved_count=resolved_count,
        blocks=len(blocks_to_rescan),
        avg_block_interval=round(avg_block_interval, 2),
        threshold_seconds=threshold_seconds,
    )


@shared_task(ignore_result=True)
def rescan_pending_evm_chains() -> None:
    """批量为所有启用的 EVM 链触发重扫，风格对齐 scan_active_evm_chains。"""
    from chains.models import Chain
    from chains.models import ChainType

    for chain_pk in Chain.objects.filter(
        active=True,
        type=ChainType.EVM,
    ).values_list("pk", flat=True):
        _rescan_pending_evm_chain.delay(chain_pk)
