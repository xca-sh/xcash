import structlog
from django.db import OperationalError
from django.db.models import CharField
from django.db.models import Exists
from django.db.models import OuterRef
from django.db.models import Value
from django.db.models.functions import Cast
from django.db.models.functions import Concat
from procrastinate.contrib.django import app
from procrastinate.contrib.django.models import ProcrastinateJob

from chains.adapters import AdapterFactory
from chains.adapters import TxCheckResult
from chains.adapters import TxCheckStatus
from chains.models import Chain
from chains.models import ConfirmMode
from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import VaultSlotCollectSchedule
from chains.scanning import deadline_passed
from chains.scanning import step_deadline
from chains.vault_slot_balances import reconcile_vault_slot_collect_balance_gaps
from common.jobs import PRIORITY_DEFAULT
from common.jobs import PRIORITY_URGENT
from common.jobs import BackoffTable
from common.jobs import defer_once
from common.time import ago

logger = structlog.get_logger()

PROCESS_TRANSFER_TASK = "chains.process_transfer"
CONFIRM_TRANSFER_TASK = "chains.confirm_transfer"
SCHEDULE_VAULT_SLOT_DEPLOY_TASK = "chains.schedule_vault_slot_deploy"

# 高并发下 try_match_invoice / confirm_invoice 等行锁链路会触发 PostgreSQL 死锁，
# PG 死锁的设计前提就是被牺牲方应重试；死锁（OperationalError）时按表退避重试，
# 避免单次失败导致 transfer 永久卡在未处理状态。其它异常不重试，由兜底派发接手。
PROCESS_TRANSFER_RETRY = BackoffTable((2, 4, 8), retry_exceptions=(OperationalError,))
# 链上查询失败与 receipt 暂不可见：8s → 16s → 32s → 64s → 128s，避免节点抖动时密集重试。
CONFIRM_TRANSFER_RETRY = BackoffTable((8, 16, 32, 64, 128))
SCHEDULE_VAULT_SLOT_DEPLOY_RETRY = BackoffTable((8, 16, 32, 64, 128))


def job_lock(task_name: str, pk: int) -> str:
    """按任务名与业务主键构造 queueing_lock：同一对象至多一个待执行任务（含等待中的重试）。"""
    return f"{task_name}:{pk}"


def pending_job(task_name: str) -> Exists:
    """子查询：OuterRef("pk") 对应的对象是否已有待执行或执行中的该类任务。

    批量入队前先排除已在队列里的对象：只靠 queueing_lock 冲突去重也正确，但每次冲突
    都会在 PG 日志里记一条唯一约束 ERROR，周期性批量入队会持续刷出噪音。极少数并发
    竞争仍由 defer_once 的冲突处理兜住。
    """
    return Exists(
        ProcrastinateJob.objects.filter(
            queueing_lock=Concat(
                Value(f"{task_name}:"),
                Cast(OuterRef("pk"), output_field=CharField()),
            ),
            status__in=("todo", "doing"),
        )
    )


def enqueue_process_transfer(transfer_id: int) -> None:
    """在当前事务内入队转账处理。"""
    defer_once(
        process_transfer,
        queueing_lock=job_lock(PROCESS_TRANSFER_TASK, transfer_id),
        transfer_id=transfer_id,
    )


def enqueue_confirm_transfer(transfer_id: int, *, priority: int) -> None:
    """在当前事务内入队转账确认；QUICK 用 URGENT 优先级，FULL 用 DEFAULT。"""
    defer_once(
        confirm_transfer,
        queueing_lock=job_lock(CONFIRM_TRANSFER_TASK, transfer_id),
        priority=priority,
        transfer_id=transfer_id,
    )


@app.task(
    name=PROCESS_TRANSFER_TASK,
    retry=PROCESS_TRANSFER_RETRY,
    priority=PRIORITY_URGENT,
)
def process_transfer(*, transfer_id: int) -> None:
    transfer = Transfer.objects.get(pk=transfer_id)
    transfer.process()


# 单轮兜底派发上限。兜底任务每 20 秒跑一次，正常情况下待处理量是个位数；一旦上游
# 积压（扫描补扫、worker 停摆后恢复），无上限的全表扫描会把成千上万条 Transfer 反复
# 投递进队列，挤占广播与确认。按 created_at 升序取批，最老的先处理，不会饿死。
FALLBACK_PROCESS_TRANSFER_BATCH_SIZE = 256


@app.task(name="chains.fallback_process_transfer")
def fallback_process_transfer(*, timestamp: int | None = None) -> None:  # noqa: ARG001
    """兜底派发 30s 前仍未处理的转账。

    入队与扫描同事务，正常情况下不会漏派；这里兜住的是重试耗尽后失败的处理任务
    （如持续死锁、处理器装配异常），已在队列里的不再重复入队。
    """
    transfer_pks = list(
        Transfer.objects.filter(
            processed_at__isnull=True,
            created_at__lte=ago(seconds=30),
        )
        .exclude(pending_job(PROCESS_TRANSFER_TASK))
        .order_by("created_at")
        .values_list("pk", flat=True)[:FALLBACK_PROCESS_TRANSFER_BATCH_SIZE]
    )
    for pk in transfer_pks:
        enqueue_process_transfer(pk)


# CONFIRMING 转账的最大观察时限。各链确认深度最多分钟级，超过该时限仍未确认，
# 只剩两种可能：链上事实已消失（reorg 丢弃/同 nonce 替换），或确认管线自身故障。
STALE_CONFIRMING_TRANSFER_MAX_AGE_HOURS = 24

# 批处理任务的协作式时间预算（秒）：只在两个处理单元之间检查（见 chains.scanning），
# 撞线即把剩余单元留给下一轮，不依赖异步注入的超时异常收尾。撞线后仍要等当前单元做完，
# 单次占用最坏为「预算 + 一个单元」：归集单元含部署判活、余额刷新、gas 估算多次 RPC，
# 最坏约 60s；reap 单元是一次链上终验，Tron 最坏约 50s。
COLLECT_SCHEDULE_BUDGET_SECONDS = 40
COLLECT_BALANCE_RECONCILE_BUDGET_SECONDS = 90
REAP_STALE_CONFIRMING_BUDGET_SECONDS = 200


# 每条超龄转账都要打一次链上终验 RPC，limit=200 时单轮可达数分钟，必须给足时间预算：
# 这个任务是「确认批次被链上已消失的转账占满」的唯一自愈手段，清理速度跟不上就等于失效。
@app.task(name="chains.reap_stale_confirming_transfers")
def reap_stale_confirming_transfers(
    *,
    timestamp: int | None = None,  # noqa: ARG001
    limit: int = 200,
) -> int:
    """清理超龄且链上已无事实的 CONFIRMING 转账，返回清理条数。

    确认调度按 timestamp 升序取批（batch 封顶）：链上已消失的转账会恒占批次
    头部，攒满一批即饿死该链所有后续确认。但绝不能只按时间删——确认管线自身
    故障超过时限时（RPC 配错、节点宕机），转账在链上仍真实存在，误删会解绑已
    支付的账单且扫描游标已过、无法重建。故删除前必须做一次链上终验：
    - MISSING：链上确无此交易，drop() 释放唯一约束；若日后真的重新打包，
      扫描器可自然重建。
    - SUCCEEDED：只是确认管线出过故障，重新派发确认任务恢复推进。
    - FAILED / 查询异常：保留并告警，本轮跳过，等下一轮观测或人工介入。
    """
    deadline = step_deadline(budget_seconds=REAP_STALE_CONFIRMING_BUDGET_SECONDS)
    stale_transfers = list(
        Transfer.objects.select_related("chain")
        .filter(
            status=TransferStatus.CONFIRMING,
            created_at__lte=ago(hours=STALE_CONFIRMING_TRANSFER_MAX_AGE_HOURS),
        )
        .order_by("created_at")[:limit]
    )
    reaped_count = 0
    for index, transfer in enumerate(stale_transfers):
        # 每天一轮；节点劣化时当天可能清不完，剩余的按 created_at 顺序次日继续。
        if index > 0 and deadline_passed(deadline):
            logger.info(
                "超龄 CONFIRMING 转账清理本轮预算用尽，剩余留待下一轮",
                processed=index,
                remaining=len(stale_transfers) - index,
            )
            break
        adapter = AdapterFactory.get_adapter(transfer.chain.type)
        raw_result = adapter.tx_result(chain=transfer.chain, tx_hash=transfer.hash)
        if isinstance(raw_result, Exception):
            logger.warning(
                "超龄 CONFIRMING 转账链上终验查询失败，本轮跳过",
                chain=transfer.chain.code,
                transfer_id=transfer.pk,
                tx_hash=transfer.hash,
                error=str(raw_result),
            )
            continue
        result = raw_result.status
        if result == TxCheckStatus.MISSING:
            logger.warning(
                "超龄 CONFIRMING 转账链上已无事实，清理释放确认批次",
                chain=transfer.chain.code,
                transfer_id=transfer.pk,
                tx_hash=transfer.hash,
                block=transfer.block,
                created_at=transfer.created_at,
            )
            transfer.drop()
            reaped_count += 1
        elif result == TxCheckStatus.SUCCEEDED:
            # 链上事实仍在，说明只是确认管线曾中断；主动补派确认，不等链高推进。
            # 业务归类未完成（processed_at 为空）时不能直接确认：type 仍为 Unmatched，
            # confirm 的业务分发会空转，之后补上的匹配再也等不到确认动作，
            # 造成链上有钱、账上无单。此时先补处理，确认交回正常调度
            # （enqueue_due_confirmations 过滤 processed_at，QUICK 由 process 内部入队）。
            if transfer.processed_at is None:
                enqueue_process_transfer(transfer.pk)
                continue
            enqueue_confirm_transfer(transfer.pk, priority=PRIORITY_DEFAULT)
        else:
            logger.warning(
                "超龄 CONFIRMING 转账链上终验结果异常，保留待人工排查",
                chain=transfer.chain.code,
                transfer_id=transfer.pk,
                tx_hash=transfer.hash,
                result=str(result),
            )
    return reaped_count


@app.task(
    name=SCHEDULE_VAULT_SLOT_DEPLOY_TASK,
    retry=SCHEDULE_VAULT_SLOT_DEPLOY_RETRY,
)
def schedule_vault_slot_deploy(*, slot_pk: int) -> None:
    """异步触发 VaultSlot 预部署调度。

    只有 EVM 原生币需要在暴露地址前预部署（receive() 的事件是入账识别的唯一依据）。
    请求线程不内联链上调用：RPC 抖动会把已经落库的下单请求打成 500；挪到这里后失败按
    退避表重试（8s → 16s → 32s → 64s → 128s），仍失败则由归集前的部署闸门兜底，不会丢部署。
    """
    from chains.vault_slots import schedule_deploy  # noqa: PLC0415

    try:
        schedule_deploy(slot_pk)
    except Exception as exc:
        logger.warning(
            "VaultSlot 预部署调度失败，退避重试",
            vault_slot_id=slot_pk,
            error=str(exc),
        )
        raise


@app.task(name="chains.execute_due_vault_slot_collect_schedules")
def execute_due_vault_slot_collect_schedules(
    *,
    timestamp: int | None = None,  # noqa: ARG001
) -> None:
    created_count = VaultSlotCollectSchedule.dispatch_due_collects(
        deadline=step_deadline(budget_seconds=COLLECT_SCHEDULE_BUDGET_SECONDS)
    )
    if created_count:
        logger.info(
            "VaultSlot 到期归集计划已创建链上任务",
            count=created_count,
        )


@app.task(name="chains.reconcile_vault_slot_collect_balance_gaps")
def reconcile_vault_slot_collect_balance_gaps_task(
    *,
    timestamp: int | None = None,  # noqa: ARG001
) -> None:
    summary = reconcile_vault_slot_collect_balance_gaps(
        deadline=step_deadline(budget_seconds=COLLECT_BALANCE_RECONCILE_BUDGET_SECONDS)
    )
    if summary["created_count"]:
        logger.info(
            "VaultSlot 余额对账已补建遗漏归集计划",
            created_count=summary["created_count"],
            dust_skipped_count=summary["dust_skipped_count"],
        )
    if summary["failed_blocked_count"]:
        logger.warning(
            "VaultSlot 余额对账发现失败归集阻塞",
            failed_blocked_count=summary["failed_blocked_count"],
            sample_balance_ids=[
                balance.pk for balance in summary["recent_failed_blocked"][:3]
            ],
        )
    if summary["error_count"]:
        logger.warning(
            "VaultSlot 余额对账存在单行补建失败",
            error_count=summary["error_count"],
            recent_errors=summary["recent_errors"][:3],
        )


class ReceiptNotVisibleError(Exception):
    """交易 receipt 暂不可见，交给重试策略稍后再查。"""


@app.task(
    name=CONFIRM_TRANSFER_TASK,
    retry=CONFIRM_TRANSFER_RETRY,
    pass_context=True,
)
def confirm_transfer(context, *, transfer_id: int) -> None:
    try:
        transfer = Transfer.objects.get(pk=transfer_id)
    except Transfer.DoesNotExist:
        # Transfer 已被 drop() 删除，无需再处理
        return
    if transfer.status == TransferStatus.CONFIRMED:
        return

    adapter = AdapterFactory.get_adapter(transfer.chain.type)
    raw_result = adapter.tx_result(chain=transfer.chain, tx_hash=transfer.hash)

    if isinstance(raw_result, Exception):
        # 链上查询失败：抛出交给重试策略按退避表重试。
        raise raw_result
    result = raw_result.status
    if result == TxCheckStatus.SUCCEEDED:
        if _refresh_transfer_chain_position_from_receipt(
            transfer=transfer,
            result=raw_result,
        ):
            return
        transfer.confirm()
    elif result == TxCheckStatus.MISSING:
        if CONFIRM_TRANSFER_RETRY.is_final_attempt(context.job.attempts):
            # 重试用尽仍不可见：保留观测到的转账，不报错；链高下次推进时会重新入队确认，
            # 链上事实真的消失则由 reap_stale_confirming_transfers 终验清理。
            logger.warning(
                "Transfer receipt missing after max retries, keeping observed transfer",
                chain=transfer.chain.code,
                transfer_id=transfer.pk,
                tx_hash=transfer.hash,
                block=transfer.block,
                block_hash=transfer.block_hash,
            )
            return
        raise ReceiptNotVisibleError(f"交易 receipt 暂不可见: {transfer.hash}")
    elif result == TxCheckStatus.FAILED:
        # 扫描器只在交易成功时才落 Transfer，走到这里说明链上事实已经变了（同 hash
        # 交易被重组后按失败重新打包）。若只抛异常，该转账会永久停在 CONFIRMING、
        # 每次链高推进都被重新入队确认；而 reap_stale_confirming_transfers 对 FAILED
        # 只告警不清理，确认永远推进不了。
        # 失败交易本就没有有效入账，按"链上无事实"丢弃并释放唯一约束；日后若再次被
        # 成功打包，扫描器可自然重建（与 reap 的 MISSING 分支同语义）。
        logger.error(
            "Transfer 对应交易链上执行失败，丢弃该转账",
            chain=transfer.chain.code,
            transfer_id=transfer.pk,
            tx_hash=transfer.hash,
            block=transfer.block,
        )
        transfer.drop()


def _refresh_transfer_chain_position_from_receipt(
    *,
    transfer: Transfer,
    result: TxCheckResult,
) -> bool:
    """receipt 的块位置变化时刷新转账，并重新等待确认窗口。

    reorg 后同一 tx_hash 可能被重新打包到不同块；若继续沿用旧 block 计算确认数，
    FULL 确认会被提前放行。block_hash 能覆盖“同一高度但不同块”的场景。
    """
    updates: dict[str, object] = {}
    if result.block_number is not None and int(result.block_number) != transfer.block:
        updates["block"] = int(result.block_number)
    if result.block_hash and result.block_hash != transfer.block_hash:
        updates["block_hash"] = result.block_hash
    if not updates:
        return False

    Transfer.objects.filter(pk=transfer.pk).update(**updates)
    return True


# 单次入队的批量上限，与兜底派发一致：停摆恢复后到期转账可能成批出现，分多次链高推进
# 补齐，避免一次插入过多任务。已在队列里的会被排除，剩余的下次推进时接着入队。
CONFIRM_ENQUEUE_BATCH_SIZE = 256


def enqueue_due_confirmations(chain: Chain) -> int:
    """把本链到期待确认的转账入队确认，返回入队条数。

    判据：只取已完成业务归类（processed_at 非空）的 CONFIRMING 转账；QUICK 不看块高，
    FULL 要达到确认深度且创建超过 10s。已有待执行或执行中确认任务的不再入队。
    QUICK 用 URGENT 优先级，FULL 用 DEFAULT。按 timestamp 升序取批，最老的先确认。
    """
    base_qs = Transfer.objects.filter(
        chain=chain,
        status=TransferStatus.CONFIRMING,
        processed_at__isnull=False,
    ).exclude(pending_job(CONFIRM_TRANSFER_TASK))

    quick_pks = list(
        base_qs.filter(confirm_mode=ConfirmMode.QUICK)
        .order_by("timestamp")
        .values_list("pk", flat=True)[:CONFIRM_ENQUEUE_BATCH_SIZE]
    )
    full_pks = list(
        base_qs.filter(
            confirm_mode=ConfirmMode.FULL,
            block__lte=chain.latest_block_number - chain.confirm_block_count,
            created_at__lte=ago(seconds=10),
        )
        .order_by("timestamp")
        .values_list("pk", flat=True)[:CONFIRM_ENQUEUE_BATCH_SIZE]
    )
    for pk in quick_pks:
        enqueue_confirm_transfer(pk, priority=PRIORITY_URGENT)
    for pk in full_pks:
        enqueue_confirm_transfer(pk, priority=PRIORITY_DEFAULT)
    return len(quick_pks) + len(full_pks)


def dispatch_block_confirmation_checks_if_needed(
    *,
    chain: Chain,
    previous_latest_block: int,
) -> None:
    """链扫描推进高度后，把到期的转账入队确认。

    链高事实由各链扫描器在同一链路内刷新；只在高度确实前进时才判定到期，空链每轮
    扫描不做多余查询。调用点必须在批量扫块之前；入队故障仅记录，不能反过来阻断扫描入库。
    """
    try:
        chain.refresh_from_db(fields=["latest_block_number"])
        if chain.latest_block_number <= previous_latest_block:
            return
        enqueue_due_confirmations(chain)
    except Exception:  # noqa: BLE001
        logger.exception("充值确认调度派发失败", chain=chain.code)
