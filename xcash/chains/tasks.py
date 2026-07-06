import structlog
from celery import shared_task
from django.db import OperationalError

from chains.adapters import AdapterFactory
from chains.adapters import TxCheckResult
from chains.adapters import TxCheckStatus
from chains.models import Chain
from chains.models import ConfirmMode
from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import VaultSlotCollectSchedule
from chains.vault_slot_balances import reconcile_vault_slot_collect_balance_gaps
from common.decorators import singleton_task
from common.time import ago

logger = structlog.get_logger()


# 高并发下 try_match_invoice / confirm_invoice 等行锁链路会触发 PostgreSQL 死锁，
# PG 死锁的设计前提就是被牺牲方应重试；这里通过 autoretry_for 让 Celery 在死锁时
# 指数退避自动重试，避免单次失败导致 transfer 永久卡在未处理状态。
@shared_task(
    ignore_result=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=10,
    retry_jitter=True,
    max_retries=3,
)
@singleton_task(timeout=5, use_params=True)
def process_transfer(pk):
    transfer = Transfer.objects.get(pk=pk)
    transfer.process()


@shared_task
def fallback_process_transfer():
    for transfer in Transfer.objects.filter(
        processed_at__isnull=True,
        created_at__lte=ago(seconds=30),
    ):
        process_transfer.delay(transfer.pk)


# CONFIRMING 转账的最大观察时限。各链确认深度最多分钟级，超过该时限仍未确认，
# 只剩两种可能：链上事实已消失（reorg 丢弃/同 nonce 替换），或确认管线自身故障。
STALE_CONFIRMING_TRANSFER_MAX_AGE_HOURS = 24


@shared_task(ignore_result=True)
@singleton_task(timeout=300)
def reap_stale_confirming_transfers(limit: int = 200) -> int:
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
    stale_transfers = (
        Transfer.objects.select_related("chain")
        .filter(
            status=TransferStatus.CONFIRMING,
            created_at__lte=ago(hours=STALE_CONFIRMING_TRANSFER_MAX_AGE_HOURS),
        )
        .order_by("created_at")[:limit]
    )
    reaped_count = 0
    for transfer in stale_transfers:
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
        result = (
            raw_result.status if isinstance(raw_result, TxCheckResult) else raw_result
        )
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
            confirm_transfer.delay(transfer.pk)
        else:
            logger.warning(
                "超龄 CONFIRMING 转账链上终验结果异常，保留待人工排查",
                chain=transfer.chain.code,
                transfer_id=transfer.pk,
                tx_hash=transfer.hash,
                result=str(result),
            )
    return reaped_count


@shared_task(ignore_result=True)
@singleton_task(timeout=55)
def execute_due_vault_slot_collect_schedules() -> None:
    created_count = VaultSlotCollectSchedule.execute_due()
    if created_count:
        logger.info(
            "VaultSlot 到期归集计划已创建链上任务",
            count=created_count,
        )


@shared_task(ignore_result=True)
@singleton_task(timeout=55)
def reconcile_vault_slot_collect_balance_gaps_task() -> None:
    summary = reconcile_vault_slot_collect_balance_gaps()
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


@shared_task(
    ignore_result=True,
    bind=True,
    max_retries=5,
    time_limit=10,
)
@singleton_task(timeout=5, use_params=True)
def confirm_transfer(self, pk):
    try:
        transfer = Transfer.objects.get(pk=pk)
    except Transfer.DoesNotExist:
        # Transfer 已被 drop() 删除，无需再处理
        return
    if transfer.status == TransferStatus.CONFIRMED:
        return

    adapter = AdapterFactory.get_adapter(transfer.chain.type)
    raw_result = adapter.tx_result(chain=transfer.chain, tx_hash=transfer.hash)

    if isinstance(raw_result, Exception):
        # 指数退避：8s → 16s → 32s → 64s → 128s，避免节点抖动时密集重试。
        countdown = 8 * (2**self.request.retries)  # noqa
        raise self.retry(exc=raw_result, countdown=countdown)  # noqa
    result_meta = raw_result if isinstance(raw_result, TxCheckResult) else None
    result = result_meta.status if result_meta is not None else raw_result
    if result == TxCheckStatus.SUCCEEDED:
        if _refresh_transfer_chain_position_from_receipt(
            transfer=transfer,
            result=result_meta,
        ):
            return
        transfer.confirm()
    elif result == TxCheckStatus.MISSING:
        if self.request.retries >= self.max_retries:  # noqa
            logger.warning(
                "Transfer receipt missing after max retries, keeping observed transfer",
                chain=transfer.chain.code,
                transfer_id=transfer.pk,
                tx_hash=transfer.hash,
                block=transfer.block,
                block_hash=transfer.block_hash,
            )
            return
        countdown = 8 * (2**self.request.retries)  # noqa
        raise self.retry(  # noqa
            exc=RuntimeError(f"交易 receipt 暂不可见: {transfer.hash}"),
            countdown=countdown,
        )
    elif result == TxCheckStatus.FAILED:
        raise RuntimeError(
            "失败交易不应存在 Transfer 记录；请检查扫描器与内部任务协调器语义"
        )


def _refresh_transfer_chain_position_from_receipt(
    *,
    transfer: Transfer,
    result: TxCheckResult | None,
) -> bool:
    """receipt 的块位置变化时刷新转账，并重新等待确认窗口。

    reorg 后同一 tx_hash 可能被重新打包到不同块；若继续沿用旧 block 计算确认数，
    FULL 确认会被提前放行。block_hash 能覆盖“同一高度但不同块”的场景。
    """
    if result is None:
        return False

    updates: dict[str, object] = {}
    if result.block_number is not None and int(result.block_number) != transfer.block:
        updates["block"] = int(result.block_number)
    if result.block_hash and result.block_hash != transfer.block_hash:
        updates["block_hash"] = result.block_hash
    if not updates:
        return False

    Transfer.objects.filter(pk=transfer.pk).update(**updates)
    return True


@shared_task(ignore_result=True)
def block_number_updated(chain_pk):
    batch_size = 16
    # confirm_block_count 已从 DB 字段瘦身为 property（按 chain 名从常量读取），
    # only() 只能列具体存量字段；chain 字段本身用于推导确认深度，必须一并加载。
    chain = Chain.objects.only("code", "latest_block_number").get(pk=chain_pk)
    base_qs = Transfer.objects.filter(
        chain=chain,
        status=TransferStatus.CONFIRMING,
        processed_at__isnull=False,
    )

    quick_pks = list(
        base_qs.filter(
            confirm_mode=ConfirmMode.QUICK,
        )
        .order_by("timestamp")[:batch_size]
        .values_list("pk", flat=True)
    )

    full_pks = list(
        base_qs.filter(
            confirm_mode=ConfirmMode.FULL,
            block__lte=chain.latest_block_number - chain.confirm_block_count,
            created_at__lte=ago(seconds=10),
        )
        .order_by("timestamp")[:batch_size]
        .values_list("pk", flat=True)
    )

    dispatched = quick_pks + full_pks
    for pk in dispatched:
        confirm_transfer.delay(pk)

    # 当任一模式满批时，可能还有积压；延迟自调度继续消化，避免大量转账等到下个区块才处理。
    if len(quick_pks) >= batch_size or len(full_pks) >= batch_size:
        block_number_updated.apply_async(args=(chain_pk,), countdown=2)


def dispatch_block_confirmation_checks_if_needed(
    *,
    chain: Chain,
    previous_latest_block: int,
) -> None:
    """链扫描推进高度后，按需派发 Transfer 确认检查。

    链高事实由各链扫描器在同一链路内刷新；确认调度只关心“高度确实前进”
    且存在已完成业务归类的 CONFIRMING 转账，避免空链每轮扫描都投递任务。
    """
    chain.refresh_from_db(fields=["latest_block_number"])
    if chain.latest_block_number <= previous_latest_block:
        return

    has_confirming_transfers = Transfer.objects.filter(
        chain=chain,
        status=TransferStatus.CONFIRMING,
        processed_at__isnull=False,
    ).exists()
    if not has_confirming_transfers:
        return

    block_number_updated.delay(chain.pk)
