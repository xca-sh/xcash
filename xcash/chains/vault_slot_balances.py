from __future__ import annotations

from decimal import Decimal

import structlog
from django.db import transaction
from django.db.models import Exists
from django.db.models import OuterRef
from django.db.models import Q
from django.utils import timezone

from chains.adapters import AdapterFactory
from chains.models import Transfer
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import VaultSlot
from chains.models import VaultSlotBalance
from chains.models import VaultSlotCollectSchedule

logger = structlog.get_logger()

ACTIVE_COLLECT_TASK_STATUSES = (TxTaskStatus.QUEUED, TxTaskStatus.SUBMITTED)


def refresh_vault_slot_balance(
    *,
    slot: VaultSlot,
    crypto,
    trigger_tx_hash: str | None = None,
    block_number: int | None = None,
) -> VaultSlotBalance:
    """读取链上余额真值，并按同步区块单调刷新 VaultSlotBalance。"""
    chain = slot.chain
    adapter = AdapterFactory.get_adapter(chain.type)
    raw_balance = adapter.get_balance(slot.address, chain, crypto)
    value = Decimal(int(raw_balance))
    amount = value.scaleb(-crypto.get_decimals(chain))
    worth = crypto.usd_amount(amount)
    synced_at = timezone.now()
    with transaction.atomic():
        locked_slot = VaultSlot.objects.select_related("chain").select_for_update().get(
            pk=slot.pk
        )
        synced_block_number = (
            block_number
            if block_number is not None
            else locked_slot.chain.latest_block_number
        )
        balance, created = VaultSlotBalance.objects.get_or_create(
            chain=locked_slot.chain,
            vault_slot=locked_slot,
            crypto=crypto,
            defaults={
                "value": value,
                "amount": amount,
                "worth": worth,
                "synced_block_number": synced_block_number,
                "synced_at": synced_at,
                "last_tx_hash": trigger_tx_hash,
            },
        )
        if created:
            return balance

        if (
            balance.synced_block_number is not None
            and synced_block_number < balance.synced_block_number
        ):
            logger.info(
                "VaultSlot 余额旧快照跳过",
                chain=locked_slot.chain.code,
                vault_slot_id=locked_slot.pk,
                crypto=getattr(crypto, "symbol", None),
                incoming_block=synced_block_number,
                existing_block=balance.synced_block_number,
                trigger_tx_hash=trigger_tx_hash,
            )
            return balance

        balance.value = value
        balance.amount = amount
        balance.worth = worth
        balance.synced_block_number = synced_block_number
        balance.synced_at = synced_at
        balance.last_tx_hash = trigger_tx_hash
        balance.save(
            update_fields=[
                "value",
                "amount",
                "worth",
                "synced_block_number",
                "synced_at",
                "last_tx_hash",
                "updated_at",
            ]
        )
    return balance


def refresh_vault_slot_balance_safely(
    *,
    slot: VaultSlot,
    crypto,
    trigger_tx_hash: str | None = None,
    block_number: int | None = None,
    reason: str,
) -> VaultSlotBalance | None:
    try:
        return refresh_vault_slot_balance(
            slot=slot,
            crypto=crypto,
            trigger_tx_hash=trigger_tx_hash,
            block_number=block_number,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "VaultSlot 余额刷新失败",
            reason=reason,
            chain=slot.chain.code,
            vault_slot_id=slot.pk,
            crypto=getattr(crypto, "symbol", None),
            error=str(exc),
        )
        return None


def refresh_vault_slot_balance_for_transfer(transfer: Transfer) -> None:
    """Transfer 确认后刷新命中的 VaultSlot 链上余额快照。"""
    slot = (
        VaultSlot.objects.select_related("chain")
        .filter(chain=transfer.chain, address__iexact=transfer.to_address)
        .order_by("pk")
        .first()
    )
    if slot is None:
        return

    refresh_vault_slot_balance_safely(
        slot=slot,
        crypto=transfer.crypto,
        trigger_tx_hash=transfer.hash,
        block_number=transfer.block,
        reason="transfer_confirm",
    )


def refresh_vault_slot_balance_for_collect_task(tx_task: TxTask) -> VaultSlotBalance | None:
    """不生成 Transfer 的归集任务确认后刷新余额。"""
    schedule = (
        VaultSlotCollectSchedule.objects.select_related(
            "chain",
            "crypto",
            "vault_slot",
            "vault_slot__chain",
        )
        .filter(tx_task=tx_task)
        .first()
    )
    if schedule is None:
        return None
    return refresh_vault_slot_balance_safely(
        slot=schedule.vault_slot,
        crypto=schedule.crypto,
        trigger_tx_hash=tx_task.tx_hash,
        block_number=tx_task.chain.latest_block_number,
        reason="collect_task_confirm",
    )


def balance_reaches_collect_threshold(balance: VaultSlotBalance) -> bool:
    """余额价值是否达到最小归集阈值（实时价现算）。

    与 execute_one_due 的 balance_worth_reaches_collect_threshold 判据同源，供安全网
    补建计划前复核，避免与 execute_one_due 就同一笔粉尘反复拉锯（建→删）：
    - 阈值为 0 视为不限制，直接放行。
    - 用实时价现算，不用 balance.worth 快照：快照在缺价时降级为 0，会把正常金额
      误判成粉尘。
    - 缺价（PriceUnavailableError）无法判定粉尘，按“达到阈值”处理，让安全网照常补建，
      由 execute_due 后续用实时价再决定归集或退避，不在这里丢弃余额。
    """
    from core.runtime_settings import get_vault_slot_collect_min_worth_usd
    from currencies.models import PriceUnavailableError

    threshold = get_vault_slot_collect_min_worth_usd()
    if threshold <= 0:
        return True
    try:
        worth = balance.amount * balance.crypto.price("USD")
    except PriceUnavailableError:
        return True
    return worth >= threshold


def vault_slot_collect_balance_gaps():
    """返回仍有余额、无 pending / 在途归集计划、且未被确证为粉尘的快照。

    价值门槛必须与 execute_one_due 对齐：低于最小归集价值的余额会被 execute_one_due
    删除计划（粉尘归集必亏 gas）。安全网若只看 value>0 就把刚删的计划立刻补建，两个
    任务会就同一笔粉尘反复拉锯（建→删）永不收敛，还持续烧链上 RPC 并挤占 limit 批次。

    这里在 SQL 层排除“已确证粉尘”（0 < worth < 阈值）：
    - execute_one_due 只在实时价现算出 worth<阈值时才删除粉尘计划，删除前的余额刷新
      已把同一 worth 写入快照，故快照落在 (0, 阈值) 与删除判据一致，粉尘被稳定排除、
      不再进入迭代，从源头消除拉锯与批次挤占。
    - worth==0 可能是“真零”或“缺价降级为 0”，SQL 无法区分，保留交由 reconcile 用
      实时价（balance_reaches_collect_threshold）复核，避免误伤缺价余额。
    - 阈值为 0（不限制）时不加价值过滤。
    """
    from core.runtime_settings import get_vault_slot_collect_min_worth_usd

    matching_schedules = VaultSlotCollectSchedule.objects.filter(
        chain_id=OuterRef("chain_id"),
        vault_slot_id=OuterRef("vault_slot_id"),
        crypto_id=OuterRef("crypto_id"),
    )
    active_schedules = matching_schedules.filter(
        Q(tx_task__isnull=True) | Q(tx_task__status__in=ACTIVE_COLLECT_TASK_STATUSES)
    )
    failed_schedules = matching_schedules.filter(tx_task__status=TxTaskStatus.FAILED)
    queryset = (
        VaultSlotBalance.objects.select_related("chain", "crypto", "vault_slot")
        .annotate(
            has_active_collect_schedule=Exists(active_schedules),
            has_failed_collect_schedule=Exists(failed_schedules),
        )
        .filter(value__gt=0, has_active_collect_schedule=False)
    )
    threshold = get_vault_slot_collect_min_worth_usd()
    if threshold > 0:
        queryset = queryset.exclude(worth__gt=0, worth__lt=threshold)
    return queryset.order_by("updated_at", "pk")


def reconcile_vault_slot_collect_balance_gaps(*, limit: int = 32) -> dict:
    """对账余额快照，补齐遗漏的归集计划并暴露失败归集的人工恢复入口。

    已存在 FAILED 归集任务时不自动重试，避免黑名单 / 永久 revert 场景被周期任务
    反复烧 gas；这类余额只输出告警，由后台 action 人工确认后重新排队。
    """
    created_count = 0
    dust_skipped = 0
    failed_blocked = []
    errors = []
    for balance in vault_slot_collect_balance_gaps()[:limit]:
        try:
            if balance.has_failed_collect_schedule:
                failed_blocked.append(balance)
                logger.warning(
                    "VaultSlot 余额仍未归集且最近存在失败归集任务，等待人工重试",
                    chain=balance.chain.code,
                    vault_slot_id=balance.vault_slot_id,
                    crypto=getattr(balance.crypto, "symbol", None),
                    balance_value=str(balance.value),
                )
                continue

            # 实时价复核：SQL 层只挡住 worth 快照已确证的粉尘，worth==0 的余额（真零
            # 或缺价降级）仍会进来。低于阈值的粉尘在此跳过、不补建，打断与
            # execute_one_due 的建删拉锯；缺价按“达到阈值”放行，交由 execute_due 复核。
            if not balance_reaches_collect_threshold(balance):
                dust_skipped += 1
                continue

            schedule = VaultSlotCollectSchedule.ensure_pending_due_now(
                chain=balance.chain,
                vault_slot=balance.vault_slot,
                crypto=balance.crypto,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {
                    "balance_id": balance.pk,
                    "chain": balance.chain.code,
                    "vault_slot_id": balance.vault_slot_id,
                    "crypto": getattr(balance.crypto, "symbol", None),
                    "error": str(exc),
                }
            )
            logger.warning(
                "VaultSlot 余额对账补建归集计划失败，跳过该余额",
                chain=balance.chain.code,
                vault_slot_id=balance.vault_slot_id,
                crypto=getattr(balance.crypto, "symbol", None),
                balance_value=str(balance.value),
                error=str(exc),
            )
            continue

        created_count += 1
        logger.info(
            "VaultSlot 余额对账已补建归集计划",
            schedule_id=schedule.pk,
            chain=balance.chain.code,
            vault_slot_id=balance.vault_slot_id,
            crypto=getattr(balance.crypto, "symbol", None),
            balance_value=str(balance.value),
        )

    return {
        "created_count": created_count,
        "dust_skipped_count": dust_skipped,
        "failed_blocked_count": len(failed_blocked),
        "recent_failed_blocked": failed_blocked,
        "error_count": len(errors),
        "recent_errors": errors,
    }
