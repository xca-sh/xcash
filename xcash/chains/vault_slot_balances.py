from __future__ import annotations

from decimal import Decimal

import structlog
from celery.exceptions import SoftTimeLimitExceeded
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
        # of=("self",) 把行锁限定在 chains_vaultslot：select_related("chain") 会把
        # chains_chain join 进来，裸 FOR UPDATE 在 PostgreSQL 下会连该链的 Chain 行
        # 一并锁死，使本链所有引用 chain 的插入（TxTask/TxHash/Transfer/余额快照）都卡在
        # FK 的 FOR KEY SHARE 上；且与「先锁 Chain 再更新 VaultSlot」的入账路径构成
        # 反向加锁顺序，会周期性触发死锁。chain 在此只用于读 latest_block_number，无需上锁。
        locked_slot = (
            VaultSlot.objects.select_related("chain")
            .select_for_update(of=("self",))
            .get(pk=slot.pk)
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
    except SoftTimeLimitExceeded:
        # SoftTimeLimitExceeded 继承自 Exception，会被下面的宽泛 except 吞掉。一旦吞掉，
        # 任务不会收尾而是继续跑下一条计划，直到撞上硬超时被 SIGKILL——那时 singleton
        # 锁不会走 finally 释放、只能等 TTL 过期，worker 子进程也被连带杀掉。
        # 软超时是「请尽快收尾」的控制信号，不是业务异常，必须原样上抛。
        raise
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
    """Transfer 确认后刷新命中的 VaultSlot 链上余额快照。

    两侧地址都经 AddressField 落库校验为规范形态（EVM checksum / Tron base58），
    精确匹配即可命中 (chain, address) 唯一索引；iexact 会退化成按链全表扫描。
    """
    slot = (
        VaultSlot.objects.select_related("chain")
        .filter(chain=transfer.chain, address=transfer.to_address)
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


def refresh_vault_slot_balance_for_collect_task(
    tx_task: TxTask,
) -> VaultSlotBalance | None:
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
    """返回仍有余额但没有 pending / 在途归集计划的快照。

    这里刻意不用 worth 快照做粉尘过滤：worth 只在余额刷新时落库、行情任务只更新
    Crypto.prices，价格上涨后旧快照不会重算——按快照排除会把“当时是粉尘、现已
    涨价达标”的余额永久挡在安全网之外（无新入账的槽位没有其他路径触发重估）。
    粉尘判定的唯一权威是 reconcile 循环内的实时价复核
    （balance_reaches_collect_threshold）；被判粉尘的行回写 worth 并推进
    updated_at 轮转到队尾，既不恒占 limit 批次头部饿死后续缺口，涨价后轮回
    队首时也会被重新放行。
    """
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
                # 轮转到队尾：gaps 按 updated_at 升序切 limit 批，原地跳过会让
                # 失败阻塞行恒占批次头部，攒满 limit 个即饿死所有后续真实缺口。
                VaultSlotBalance.objects.filter(pk=balance.pk).update(
                    updated_at=timezone.now()
                )
                continue

            # 实时价复核（粉尘判定的唯一权威，与 execute_one_due 判据同源）：低于
            # 阈值的粉尘不补建计划，打断与 execute_one_due 的建删拉锯；缺价按
            # “达到阈值”放行，交由 execute_due 复核。跳过时回写实时 worth 快照并
            # 推进 updated_at 轮转到队尾——否则粉尘恒占批次头部饿死后续缺口；
            # 涨价后轮回队首时会被本复核重新放行，粉尘不会被永久排除。
            if not balance_reaches_collect_threshold(balance):
                dust_skipped += 1
                VaultSlotBalance.objects.filter(pk=balance.pk).update(
                    worth=balance.crypto.usd_amount(balance.amount),
                    updated_at=timezone.now(),
                )
                continue

            schedule = VaultSlotCollectSchedule.ensure_pending_due_now(
                chain=balance.chain,
                vault_slot=balance.vault_slot,
                crypto=balance.crypto,
            )
        except SoftTimeLimitExceeded:
            # 同上：软超时不能被逐行的容错 except 吞掉，否则整批会一直跑到硬超时被杀。
            raise
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
            # 持续异常的单行也要轮转到队尾，避免占满 limit 批次后
            # 饿死后续可正常补建的余额缺口。
            VaultSlotBalance.objects.filter(pk=balance.pk).update(
                updated_at=timezone.now()
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
