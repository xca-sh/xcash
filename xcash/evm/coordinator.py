from __future__ import annotations

import structlog
from django.db import transaction as db_transaction
from web3.exceptions import TransactionNotFound

from chains.adapters import TxCheckStatus
from chains.models import BroadcastTask
from chains.models import BroadcastTaskFailureReason
from chains.models import BroadcastTaskResult
from chains.models import BroadcastTaskStage
from chains.models import Chain
from common.time import ago
from evm.constants import EVM_PENDING_REBROADCAST_TIMEOUT
from evm.models import EvmBroadcastTask

logger = structlog.get_logger()


class InternalEvmTaskCoordinator:
    """协调内部 EVM 任务的链上终局状态。

    对 PENDING_CHAIN 超过阈值仍未终局的任务，遍历所有历史 tx_hash 查询 receipt：
    - 查到 receipt (status=1) -> 构建 ObservedTransferPayload 喂回扫描器管线
    - 查到 receipt (status=0) -> 标记失败终局
    - 所有 hash 均无 receipt -> 交易已被 mempool 丢弃，重新广播
    """

    @classmethod
    def reconcile_chain(cls, *, chain: Chain) -> None:
        queryset = (
            EvmBroadcastTask.objects.select_related("base_task", "address")
            .filter(
                chain=chain,
                base_task__stage=BroadcastTaskStage.PENDING_CHAIN,
                base_task__result=BroadcastTaskResult.UNKNOWN,
                last_attempt_at__lt=ago(seconds=EVM_PENDING_REBROADCAST_TIMEOUT),
            )
            .order_by("address_id", "nonce", "created_at")
        )

        for evm_task in queryset:
            if not evm_task.base_task_id:
                continue

            status, tx_hash, receipt = cls._find_receipt_across_hashes(evm_task=evm_task)
            if isinstance(status, Exception):
                logger.warning(
                    "EVM 任务超时收口查链失败",
                    chain=chain.code,
                    address=evm_task.address.address,
                    nonce=evm_task.nonce,
                    error=str(status),
                )
                continue

            if status == TxCheckStatus.CONFIRMED:
                assert tx_hash is not None  # CONFIRMED 分支一定携带命中的 hash
                assert receipt is not None
                try:
                    cls._observe_confirmed_transaction(
                        evm_task=evm_task, tx_hash=tx_hash, receipt=receipt,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "协调器观察确认交易失败",
                        chain=chain.code,
                        address=evm_task.address.address,
                        nonce=evm_task.nonce,
                        tx_hash=tx_hash,
                    )
                    continue
            elif status == TxCheckStatus.FAILED:
                cls._finalize_failed_task(evm_task=evm_task)
            else:
                # 所有历史 hash 都找不到 receipt，交易已被 mempool 丢弃，重新广播。
                try:
                    evm_task.broadcast(allow_pending_chain_rebroadcast=True)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "PENDING_CHAIN 超时重新广播失败",
                        chain=chain.code,
                        address=evm_task.address.address,
                        nonce=evm_task.nonce,
                    )
                else:
                    logger.info(
                        "PENDING_CHAIN 超时且无链上记录，已重新广播",
                        chain=chain.code,
                        address=evm_task.address.address,
                        nonce=evm_task.nonce,
                    )

    @staticmethod
    def _find_receipt_across_hashes(
        *, evm_task: EvmBroadcastTask
    ) -> tuple[TxCheckStatus | Exception, str | None, dict | None]:
        """遍历任务的所有历史 tx_hash 查找链上 receipt。

        返回 (status, tx_hash, receipt):
        - 找到 receipt -> (CONFIRMED 或 FAILED, 命中的 hash, receipt)
        - 全部未找到 -> (CONFIRMING, None, None)
        - RPC 异常 -> (Exception, None, None)
        """
        for tx_hash in evm_task._known_tx_hashes():
            try:
                receipt = evm_task.chain.w3.eth.get_transaction_receipt(tx_hash)
            except TransactionNotFound:
                continue
            except Exception as exc:  # noqa: BLE001
                return exc, None, None

            if receipt is None:
                continue

            status = receipt.get("status")
            if status == 1:
                return TxCheckStatus.CONFIRMED, tx_hash, dict(receipt)
            if status == 0:
                return TxCheckStatus.FAILED, tx_hash, None
            return RuntimeError("EVM receipt status missing or invalid"), None, None

        return TxCheckStatus.CONFIRMING, None, None

    @staticmethod
    def _observe_confirmed_transaction(
        *, evm_task: EvmBroadcastTask, tx_hash: str, receipt: dict,
    ) -> None:
        """协调器兜底命中 receipt 时，把交易交给内部处理器统一推进。"""
        from evm.internal_tx.processor import process_internal_transaction

        chain = evm_task.chain
        tx = chain.w3.eth.get_transaction(tx_hash)
        process_internal_transaction(chain=chain, tx=dict(tx), receipt=receipt)

    @staticmethod
    @db_transaction.atomic
    def _finalize_failed_task(*, evm_task: EvmBroadcastTask) -> bool:
        from evm.internal_tx.handlers import get_handler

        locked_task = EvmBroadcastTask.objects.select_for_update().get(pk=evm_task.pk)
        if not locked_task.base_task_id:
            return False

        base_task = locked_task.base_task
        if (
            base_task.stage != BroadcastTaskStage.PENDING_CHAIN
            or base_task.result != BroadcastTaskResult.UNKNOWN
        ):
            return False

        updated = BroadcastTask.mark_finalized_failed(
            task_id=base_task.pk,
            reason=BroadcastTaskFailureReason.EXECUTION_REVERTED,
            expected_stage=BroadcastTaskStage.PENDING_CHAIN,
        )
        if not updated:
            return False

        try:
            handler = get_handler(base_task.action_type)
        except KeyError:
            logger.warning(
                "coordinator 收口失败但 handler 未注册",
                action_type=base_task.action_type,
                base_task_id=base_task.pk,
            )
            return True
        handler.finalize_failed(base_task, BroadcastTaskFailureReason.EXECUTION_REVERTED)
        return True
