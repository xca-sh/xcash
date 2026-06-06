from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from django.db import IntegrityError
from django.db import transaction

from chains.models import Address
from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from chains.models import ConfirmMode
from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import TransferType
from chains.models import TxTask

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal

    from currencies.models import Crypto

logger = structlog.get_logger()


class ChainService:
    """Read-only accessors for chain metadata."""

    @staticmethod
    def get_by_code(code: str, *, active_only: bool = True) -> Chain:
        qs = Chain.objects.filter(code=code)
        if active_only:
            qs = qs.filter(active=True)
        return qs.get()

    @staticmethod
    def codes_of_types(chain_types: set[str]) -> set[str]:
        return set(
            Chain.objects.filter(type__in=chain_types, active=True).values_list(
                "code", flat=True
            )
        )


class AddressService:
    """Query and mutation helpers for chain addresses."""

    @staticmethod
    def find_by_address(
        *,
        address: str,
        chain_type: ChainType | str | None = None,
        usage: AddressUsage | str | None = None,
    ) -> Address | None:
        qs = Address.objects.filter(address=address)
        if chain_type:
            qs = qs.filter(chain_type=chain_type)
        if usage:
            qs = qs.filter(usage=usage)
        return qs.first()


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
        """在事务提交后异步处理 Transfer，替代隐式 post_save signal。"""
        from chains.tasks import process_transfer

        transaction.on_commit(
            lambda transfer_id=transfer.pk: process_transfer.apply_async(
                (transfer_id,), countdown=1
            )
        )

    @staticmethod
    def _mark_tx_task_pending_confirm(*, chain: Chain, tx_hash: str) -> None:
        # 一旦链上已经观察到真实交易，统一父任务就进入"确认中"阶段。
        TxTask.mark_pending_confirm(chain=chain, tx_hash=tx_hash)

    @staticmethod
    def _schedule_deploy_after_observed_token_inbound(
        *,
        chain: Chain,
        observed: ObservedTransferPayload,
    ) -> None:
        """token 首次打入预测 VaultSlot 后调度合约部署。

        原生币入账必须在暴露地址前完成部署；能走到 scanner 的 native 日志已经来自
        已部署合约。ERC20/TRC20 入账则可先打到 CREATE2 预测地址，命中系统 VaultSlot
        后再部署，避免悬空地址提前消耗 gas / energy。
        """
        from chains.models import VaultSlot  # noqa: PLC0415

        if observed.crypto.pk == chain.native_coin.pk:
            return

        slot = (
            VaultSlot.objects.filter(
                chain=chain,
                address=observed.to_address,
                is_deployed=False,
            )
            .only("pk")
            .first()
        )
        if slot is None:
            return
        transaction.on_commit(lambda slot_pk=slot.pk: VaultSlot.schedule_deploy(slot_pk))

    @staticmethod
    def _build_observed_transfer_kwargs(
        observed: ObservedTransferPayload,
    ) -> dict[str, object]:
        return {
            "chain": observed.chain,
            "block": observed.block,
            "block_hash": observed.block_hash,
            "hash": observed.tx_hash,
            "from_address": observed.from_address,
            "to_address": observed.to_address,
            "crypto": observed.crypto,
            "value": observed.value,
            "amount": observed.amount,
            "timestamp": observed.timestamp,
            "datetime": observed.datetime,
        }

    @staticmethod
    def _refresh_confirmed_quick_observation(
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
            chain = Chain.objects.select_for_update().get(pk=observed.chain.pk)
            existing = (
                Transfer.objects.select_for_update()
                .filter(chain=chain, hash=observed.tx_hash)
                .first()
            )
            if existing is not None:
                if (
                    existing.confirm_mode == ConfirmMode.QUICK
                    and existing.status == TransferStatus.CONFIRMED
                ):
                    updated_fields = (
                        TransferService._refresh_confirmed_quick_observation(
                            transfer=existing,
                            observed=observed,
                        )
                    )
                    logger.debug(
                        "Observed QUICK confirmed transfer tx replay refreshed",
                        source=observed.source,
                        chain=chain.code,
                        tx_hash=observed.tx_hash,
                        transfer_id=existing.pk,
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
                    TransferService._mark_tx_task_pending_confirm(
                        chain=chain,
                        tx_hash=observed.tx_hash,
                    )
                    TransferService._schedule_deploy_after_observed_token_inbound(
                        chain=chain,
                        observed=observed,
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
                TransferService._mark_tx_task_pending_confirm(
                    chain=chain,
                    tx_hash=observed.tx_hash,
                )
                TransferService._schedule_deploy_after_observed_token_inbound(
                    chain=chain,
                    observed=observed,
                )
                return ObservedTransferCreateResult(transfer=transfer, created=True)
            except IntegrityError:
                existing = Transfer.objects.filter(
                    chain=chain,
                    hash=observed.tx_hash,
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

                TransferService._mark_tx_task_pending_confirm(
                    chain=chain,
                    tx_hash=observed.tx_hash,
                )
                TransferService._schedule_deploy_after_observed_token_inbound(
                    chain=chain,
                    observed=observed,
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

    @staticmethod
    def mark_confirmed(transfer: Transfer) -> Transfer:
        if transfer.status == TransferStatus.CONFIRMED:
            return transfer
        transfer.status = TransferStatus.CONFIRMED
        transfer.save(update_fields=["status"])
        return transfer
