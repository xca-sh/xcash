from celery import shared_task
from django.db import OperationalError

from chains.adapters import AdapterFactory
from chains.adapters import TxCheckResult
from chains.adapters import TxCheckStatus
from chains.models import Chain
from chains.models import ConfirmMode
from chains.models import OnchainTransfer
from chains.models import TransferStatus
from common.decorators import singleton_task
from common.time import ago


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
    transfer = OnchainTransfer.objects.get(pk=pk)
    transfer.process()


@shared_task
def fallback_process_transfer():
    for transfer in OnchainTransfer.objects.filter(
        processed_at__isnull=True,
        created_at__lte=ago(seconds=30),
    ):
        process_transfer.delay(transfer.pk)


@shared_task(
    ignore_result=True,
    bind=True,
    max_retries=5,
    time_limit=10,
)
@singleton_task(timeout=5, use_params=True)
def confirm_transfer(self, pk):
    try:
        transfer = OnchainTransfer.objects.get(pk=pk)
    except OnchainTransfer.DoesNotExist:
        # OnchainTransfer 已被 drop() 删除，无需再处理
        return
    if transfer.status == TransferStatus.CONFIRMED:
        return

    adapter = AdapterFactory.get_adapter(transfer.chain.type)
    raw_result = adapter.tx_result(chain=transfer.chain, tx_hash=transfer.hash)

    if isinstance(raw_result, Exception):
        # 指数退避：8s → 16s → 32s → 64s → 128s，避免节点抖动时密集重试。
        countdown = 8 * (2**self.request.retries)
        raise self.retry(exc=raw_result, countdown=countdown)
    result_meta = raw_result if isinstance(raw_result, TxCheckResult) else None
    result = result_meta.status if result_meta is not None else raw_result
    if result == TxCheckStatus.CONFIRMED:
        if _refresh_transfer_chain_position_from_receipt(
            transfer=transfer,
            result=result_meta,
        ):
            return
        transfer.confirm()
    elif result == TxCheckStatus.CONFIRMING:
        if self.request.retries >= self.max_retries:
            transfer.drop()
            return
        countdown = 8 * (2**self.request.retries)
        raise self.retry(
            exc=RuntimeError(f"交易 receipt 暂不可见: {transfer.hash}"),
            countdown=countdown,
        )
    elif result == TxCheckStatus.DROPPED:
        transfer.drop()
    elif result == TxCheckStatus.FAILED:
        raise RuntimeError(
            "失败交易不应存在 OnchainTransfer 记录；请检查扫描器与内部任务协调器语义"
        )


def _refresh_transfer_chain_position_from_receipt(
    *,
    transfer: OnchainTransfer,
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

    OnchainTransfer.objects.filter(pk=transfer.pk).update(**updates)
    return True


@shared_task(ignore_result=True)
def block_number_updated(chain_pk):
    batch_size = 16
    chain = Chain.objects.only("confirm_block_count", "latest_block_number").get(
        pk=chain_pk
    )
    base_qs = OnchainTransfer.objects.filter(
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


@shared_task(ignore_result=True, time_limit=40)
@singleton_task(timeout=5, use_params=True)
def update_the_latest_block(pk):
    chain = Chain.objects.get(pk=pk)
    old_latest_block = chain.latest_block_number

    chain.latest_block_number = chain.get_latest_block_number
    # 链高度刷新不依赖 save() 信号，直接 update 可减少实例级整行写入。
    Chain.objects.filter(pk=chain.pk).update(
        latest_block_number=chain.latest_block_number
    )

    if chain.latest_block_number > old_latest_block:
        block_number_updated.delay(chain.pk)


@shared_task
@singleton_task(timeout=5)
def update_latest_block():
    for chain in Chain.objects.filter(active=True):
        update_the_latest_block.delay(chain.pk)
