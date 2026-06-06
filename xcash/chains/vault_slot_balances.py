from __future__ import annotations

from decimal import Decimal

import structlog
from django.db import transaction
from django.utils import timezone

from chains.adapters import AdapterFactory
from chains.models import Transfer
from chains.models import TransferType
from chains.models import TxTask
from chains.models import VaultSlot
from chains.models import VaultSlotBalance
from chains.models import VaultSlotCollectSchedule

logger = structlog.get_logger()


def refresh_vault_slot_balance(
    *,
    slot: VaultSlot,
    crypto,
    trigger_tx_hash: str | None = None,
    block_number: int | None = None,
) -> VaultSlotBalance:
    """读取链上余额真值并覆盖写入 VaultSlotBalance。"""
    chain = slot.chain
    adapter = AdapterFactory.get_adapter(chain.type)
    raw_balance = adapter.get_balance(slot.address, chain, crypto)
    value = Decimal(int(raw_balance))
    amount = value.scaleb(-crypto.get_decimals(chain))
    synced_at = timezone.now()
    synced_block_number = (
        block_number if block_number is not None else chain.latest_block_number
    )

    defaults = {
        "value": value,
        "amount": amount,
        "synced_block_number": synced_block_number,
        "synced_at": synced_at,
        "last_tx_hash": trigger_tx_hash,
    }
    with transaction.atomic():
        locked_slot = VaultSlot.objects.select_related("chain").select_for_update().get(
            pk=slot.pk
        )
        balance, _created = VaultSlotBalance.objects.update_or_create(
            chain=locked_slot.chain,
            vault_slot=locked_slot,
            crypto=crypto,
            defaults=defaults,
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


def refresh_vault_slot_balances_for_transfer(transfer: Transfer) -> None:
    """Transfer 确认后刷新受影响 VaultSlot 的链上余额快照。"""
    slots = list(
        VaultSlot.objects.select_related("chain")
        .filter(chain=transfer.chain, address=transfer.to_address)
        .order_by("pk")[:1]
    )
    if transfer.type == TransferType.Collect:
        source_slot = (
            VaultSlot.objects.select_related("chain")
            .filter(chain=transfer.chain, address=transfer.from_address)
            .first()
        )
        if source_slot is not None and all(slot.pk != source_slot.pk for slot in slots):
            slots.append(source_slot)

    for slot in slots:
        refresh_vault_slot_balance_safely(
            slot=slot,
            crypto=transfer.crypto,
            trigger_tx_hash=transfer.hash,
            block_number=transfer.block,
            reason="transfer_confirm",
        )


def refresh_vault_slot_balance_for_collect_task(tx_task: TxTask) -> VaultSlotBalance | None:
    """Tron 等不生成 Transfer(Collect) 的归集任务确认后刷新余额。"""
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
