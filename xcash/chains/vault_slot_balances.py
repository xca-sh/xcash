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


def vault_slot_collect_balance_gaps():
    """返回仍有余额但没有 pending / 在途归集计划的快照。"""
    matching_schedules = VaultSlotCollectSchedule.objects.filter(
        chain_id=OuterRef("chain_id"),
        vault_slot_id=OuterRef("vault_slot_id"),
        crypto_id=OuterRef("crypto_id"),
    )
    active_schedules = matching_schedules.filter(
        Q(tx_task__isnull=True) | Q(tx_task__status__in=ACTIVE_COLLECT_TASK_STATUSES)
    )
    failed_schedules = matching_schedules.filter(tx_task__status=TxTaskStatus.FAILED)
    return (
        VaultSlotBalance.objects.select_related("chain", "crypto", "vault_slot")
        .annotate(
            has_active_collect_schedule=Exists(active_schedules),
            has_failed_collect_schedule=Exists(failed_schedules),
        )
        .filter(value__gt=0, has_active_collect_schedule=False)
        .order_by("updated_at", "pk")
    )


def reconcile_vault_slot_collect_balance_gaps(*, limit: int = 32) -> dict:
    """对账余额快照，补齐遗漏的归集计划并暴露失败归集的人工恢复入口。

    已存在 FAILED 归集任务时不自动重试，避免黑名单 / 永久 revert 场景被周期任务
    反复烧 gas；这类余额只输出告警，由后台 action 人工确认后重新排队。
    """
    created_count = 0
    failed_blocked = []
    for balance in vault_slot_collect_balance_gaps()[:limit]:
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

        schedule = VaultSlotCollectSchedule.ensure_pending_due_now(
            chain=balance.chain,
            vault_slot=balance.vault_slot,
            crypto=balance.crypto,
        )
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
        "failed_blocked_count": len(failed_blocked),
        "recent_failed_blocked": failed_blocked,
    }
