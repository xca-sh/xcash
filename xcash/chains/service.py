from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from django.db import DataError
from django.db import IntegrityError
from django.db import transaction

from chains.models import Chain
from chains.models import ConfirmMode
from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import TransferType

if TYPE_CHECKING:
    from datetime import datetime

    from currencies.models import Crypto

logger = structlog.get_logger()

MAX_TRANSFER_VALUE = Decimal("9" * 32)


class ChainService:
    """Read-only accessors for chain metadata."""

    @staticmethod
    def get_by_code(code: str, *, active_only: bool = True) -> Chain:
        qs = Chain.objects.filter(code=code)
        if active_only:
            qs = qs.filter(active=True)
        return qs.get()


@dataclass(frozen=True)
class ObservedTransferPayload:
    """统一描述"从链上观察到的一笔业务入账交易"。

    不同扫描器或外部观察器看到的本质都是同一类交易级事实。
    先把输入模型收口，后续新增扫描器时只需负责解析，不再重复定义 Transfer 落库字段。
    """

    chain: Chain
    block: int
    block_hash: str
    tx_hash: str
    from_address: str
    to_address: str
    crypto: Crypto
    value: Decimal
    amount: Decimal
    timestamp: int
    datetime: datetime
    source: str = "observer"
    event_index: int | None = None


@dataclass(frozen=True)
class ObservedTransferCreateResult:
    """统一描述链上转账落库结果。

    created=True:
        本次首次创建成功
    created=False & conflict=False:
        幂等重放，同一链上事件已存在
    created=False & conflict=True:
        创建失败且无法定位到同一链上事件，需要上层记录异常
    """

    transfer: Transfer | None
    created: bool
    conflict: bool = False


class TransferService:
    """Centralized mutators for Transfer to limit cross-app coupling."""

    @staticmethod
    def enqueue_processing(transfer: Transfer) -> None:
        """在落库事务内入队 Transfer 业务处理，替代隐式 post_save signal。

        与转账同一事务提交：提交前 worker 看不到任务，不会处理到尚未提交的转账；
        提交后崩溃也不会丢任务。
        """
        from chains.tasks import enqueue_process_transfer

        enqueue_process_transfer(transfer.pk)

    @staticmethod
    def _build_observed_transfer_kwargs(
        observed: ObservedTransferPayload,
    ) -> dict[str, object]:
        return {
            "chain": observed.chain,
            "block": observed.block,
            "block_hash": observed.block_hash,
            "hash": observed.tx_hash,
            "event_index": observed.event_index,
            "from_address": observed.from_address,
            "to_address": observed.to_address,
            "crypto": observed.crypto,
            "value": observed.value,
            "amount": observed.amount,
            "timestamp": observed.timestamp,
            "datetime": observed.datetime,
        }

    @staticmethod
    def _refresh_confirmed_observation(
        *,
        transfer: Transfer,
        observed: ObservedTransferPayload,
    ) -> list[str]:
        updates: dict[str, object] = {}
        if transfer.block != observed.block:
            updates["block"] = observed.block
        if transfer.block_hash != observed.block_hash:
            updates["block_hash"] = observed.block_hash
        if transfer.timestamp != observed.timestamp:
            updates["timestamp"] = observed.timestamp
        if transfer.datetime != observed.datetime:
            updates["datetime"] = observed.datetime

        if not updates:
            return []

        Transfer.objects.filter(pk=transfer.pk).update(**updates)
        for field, value in updates.items():
            setattr(transfer, field, value)
        return list(updates)

    @staticmethod
    def create_observed_transfer(
        *,
        observed: ObservedTransferPayload,
    ) -> ObservedTransferCreateResult:
        """统一入口：将"外部服务商 / 内部扫描"观察到的链上转账写入 Transfer。

        Transfer 创建不能分散在各链 provider/scanner 内部各写各的。
        后续无论是 EVM 自扫还是其他链监听，都应通过这个入口落库，
        以统一幂等语义、唯一键冲突判定和后续扩展能力。
        """
        with transaction.atomic():
            # 这里不对 Chain 行加锁。需要串行化的只是同一笔链上事件的「查已存在 →
            # reorg drop → 重建」，而 (chain, hash, event_index) 唯一约束加下方的
            # IntegrityError 回查已经把并发完整收口：两个 worker 同时重建时必然只有
            # 一个 INSERT 成功，另一个走回查分支拿到同一行。
            # 反之链级排他锁的粒度远大于所需——这是所有链、所有扫描器落库的唯一入口，
            # 每观察到一笔入账就独占该链的 Chain 行，会让本链全部外键插入（TxTask /
            # TxHash / VaultSlotBalance / 归集计划）卡在 FK 的 FOR KEY SHARE 上；
            # 被外层事务包裹时（EVM 内部交易收口）这把链锁还会一直持有到业务撮合与
            # 确认走完，且与「先锁 VaultSlot 再读 Chain」的余额刷新路径构成反向加锁
            # 顺序，周期性触发死锁。
            chain = observed.chain
            existing = (
                Transfer.objects.select_for_update()
                .filter(
                    chain=chain,
                    hash=observed.tx_hash,
                    event_index=observed.event_index,
                )
                .first()
            )
            if existing is not None:
                if existing.status == TransferStatus.CONFIRMED:
                    chain_position_changed = (
                        existing.block != observed.block
                        or existing.block_hash != observed.block_hash
                    )
                    updated_fields = TransferService._refresh_confirmed_observation(
                        transfer=existing,
                        observed=observed,
                    )
                    log = logger.warning if chain_position_changed else logger.debug
                    log(
                        "Observed confirmed transfer tx replay refreshed",
                        source=observed.source,
                        chain=chain.code,
                        tx_hash=observed.tx_hash,
                        transfer_id=existing.pk,
                        confirm_mode=existing.confirm_mode,
                        updated_fields=updated_fields,
                    )
                    return ObservedTransferCreateResult(
                        transfer=existing,
                        created=False,
                    )

                if (
                    existing.block == observed.block
                    and existing.block_hash == observed.block_hash
                ):
                    logger.debug(
                        "Observed transfer replay ignored",
                        source=observed.source,
                        chain=chain.code,
                        tx_hash=observed.tx_hash,
                        transfer_id=existing.pk,
                    )
                    return ObservedTransferCreateResult(
                        transfer=existing,
                        created=False,
                    )

                logger.warning(
                    "Observed transfer tx reorg detected",
                    source=observed.source,
                    chain=chain.code,
                    tx_hash=observed.tx_hash,
                    incoming_block=observed.block,
                    incoming_block_hash=observed.block_hash,
                    dropped_transfer_id=existing.pk,
                )
                existing.drop()

            create_kwargs = TransferService._build_observed_transfer_kwargs(observed)
            create_kwargs["chain"] = chain
            try:
                # 唯一键冲突会触发 IntegrityError；用内层 savepoint 包住，避免外层事务直接进入 broken 状态。
                with transaction.atomic():
                    transfer = Transfer.objects.create(**create_kwargs)
                # 只有首次真正落库成功的观测转账才需要派发一次业务处理任务。
                TransferService.enqueue_processing(transfer)
                return ObservedTransferCreateResult(transfer=transfer, created=True)
            except DataError as exc:
                logger.warning(
                    "Observed transfer value outside database range",
                    source=observed.source,
                    chain=chain.code,
                    tx_hash=observed.tx_hash,
                    event_index=observed.event_index,
                    value=str(observed.value),
                    amount=str(observed.amount),
                    error=str(exc),
                )
                return ObservedTransferCreateResult(
                    transfer=None,
                    created=False,
                    conflict=True,
                )
            except IntegrityError:
                existing = Transfer.objects.filter(
                    chain=chain,
                    hash=observed.tx_hash,
                    event_index=observed.event_index,
                ).first()
                if existing is None:
                    logger.warning(
                        "Observed transfer integrity conflict without existing row",
                        source=observed.source,
                        chain=chain.code,
                        tx_hash=observed.tx_hash,
                    )
                    return ObservedTransferCreateResult(
                        transfer=None,
                        created=False,
                        conflict=True,
                    )

                logger.debug(
                    "Observed transfer replay ignored",
                    source=observed.source,
                    chain=chain.code,
                    tx_hash=observed.tx_hash,
                    transfer_id=existing.pk,
                )
                return ObservedTransferCreateResult(
                    transfer=existing,
                    created=False,
                )

    @staticmethod
    def assign_type_and_mode(
        transfer: Transfer,
        transfer_type: TransferType,
        confirm_mode: ConfirmMode,
    ) -> Transfer:
        transfer.type = transfer_type
        transfer.confirm_mode = confirm_mode
        transfer.save(update_fields=["type", "confirm_mode"])
        return transfer
