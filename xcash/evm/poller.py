from __future__ import annotations

import structlog
from django.db import transaction as db_transaction
from web3.exceptions import TransactionNotFound

from chains.adapters import TxCheckStatus
from chains.models import Chain
from chains.models import TxTask
from chains.models import TxTaskStage
from common.time import ago
from evm.constants import EVM_PENDING_REBROADCAST_TIMEOUT
from evm.models import EvmTxTask

logger = structlog.get_logger()


class EvmTaskPoller:
    """轮询内部 EVM 任务的链上终局状态。

    对 PENDING_CHAIN 超过阈值仍未终局的任务，遍历所有历史 tx_hash 查询 receipt：
    - 查到 receipt (status=1) -> 构建 ObservedTransferPayload 喂回扫描器管线
    - 查到 receipt (status=0) -> 标记失败终局
    - 所有 hash 均无 receipt -> 交易已被 mempool 丢弃，重新广播
    """

    @classmethod
    def poll_chain(cls, *, chain: Chain) -> None:
        queryset = (
            EvmTxTask.objects.select_related("base_task", "address")
            .filter(
                chain=chain,
                base_task__stage=TxTaskStage.PENDING_CHAIN,
                base_task__success__isnull=True,
                last_attempt_at__lt=ago(seconds=EVM_PENDING_REBROADCAST_TIMEOUT),
            )
            .order_by("address_id", "nonce", "created_at")
        )

        for evm_task in queryset:

            status, tx_hash, receipt = cls._find_receipt_across_hashes(
                evm_task=evm_task
            )
            if isinstance(status, Exception):
                logger.warning(
                    "EVM 任务轮询查链失败",
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
                        evm_task=evm_task,
                        tx_hash=tx_hash,
                        receipt=receipt,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "轮询器观察确认交易失败",
                        chain=chain.code,
                        address=evm_task.address.address,
                        nonce=evm_task.nonce,
                        tx_hash=tx_hash,
                    )
                    continue
            elif status == TxCheckStatus.FAILED:
                try:
                    cls._finalize_failed_task(evm_task=evm_task)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "轮询器收口失败交易异常",
                        chain=chain.code,
                        address=evm_task.address.address,
                        nonce=evm_task.nonce,
                    )
                    continue
            else:
                # 所有历史 hash 都找不到 receipt，交易已被 mempool 丢弃，重新广播。
                try:
                    evm_task.broadcast(allow_pending_chain_rebroadcast=True)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "PENDING_CHAIN 超时重新执行失败",
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
        *, evm_task: EvmTxTask
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
        *,
        evm_task: EvmTxTask,
        tx_hash: str,
        receipt: dict,
    ) -> None:
        """轮询命中 receipt 时，把交易交给内部处理器统一推进。"""
        from evm.internal_tx.processor import process_internal_transaction

        chain = evm_task.chain
        tx = chain.w3.eth.get_transaction(tx_hash)
        process_internal_transaction(chain=chain, tx=dict(tx), receipt=receipt)

    @staticmethod
    @db_transaction.atomic
    def _finalize_failed_task(*, evm_task: EvmTxTask) -> bool:
        from evm.internal_tx.routing import get_handler

        locked_task = EvmTxTask.objects.select_for_update().get(pk=evm_task.pk)

        base_task = locked_task.base_task
        if (
            base_task.stage != TxTaskStage.PENDING_CHAIN
            or base_task.success is not None
        ):
            return False

        updated = TxTask.mark_finalized_failed(
            task_id=base_task.pk,
            expected_stage=TxTaskStage.PENDING_CHAIN,
        )
        if not updated:
            return False

        try:
            handler = get_handler(base_task.tx_type)
        except KeyError:
            logger.warning(
                "poller 收口失败但 handler 未注册",
                tx_type=base_task.tx_type,
                base_task_id=base_task.pk,
            )
            return True
        handler.finalize_failed(base_task)
        return True
