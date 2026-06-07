from __future__ import annotations

import structlog
from django.db import transaction as db_transaction
from web3.exceptions import TransactionNotFound

from chains.adapters import TxCheckStatus
from chains.models import Chain
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import TxTaskType
from chains.vault_slots import mark_deployed_if_on_chain_for_task
from common.time import ago
from evm.constants import EVM_PENDING_REBROADCAST_TIMEOUT
from evm.constants import EVM_PENDING_RECEIPT_POLL_DELAY
from evm.models import EvmTxTask

logger = structlog.get_logger()


class EvmTaskPoller:
    """轮询内部 EVM 任务的链上终局状态。

    对 PENDING_CHAIN 超过短轮询延迟仍未终局的任务，遍历所有历史 tx_hash 查询 receipt：
    - 查到 receipt (status=1) 且确认数达标 -> 交给内部交易处理器按 TxTask 收口
    - 查到 receipt (status=0) -> 标记失败终局
    - 所有 hash 均无 receipt 且超过重播阈值 -> 交易可能已被 mempool 丢弃，重新广播
    """

    @classmethod
    def poll_chain(cls, *, chain: Chain) -> None:
        queryset = (
            EvmTxTask.objects.select_related("base_task", "sender")
            .filter(
                chain=chain,
                base_task__status=TxTaskStatus.PENDING_CHAIN,
                last_attempt_at__lt=ago(seconds=EVM_PENDING_RECEIPT_POLL_DELAY),
            )
            .order_by("sender_id", "nonce", "created_at")
        )

        for evm_task in queryset:

            status, tx_hash, receipt = cls._find_receipt_across_hashes(
                evm_task=evm_task
            )
            if isinstance(status, Exception):
                logger.warning(
                    "EVM 任务轮询查链失败",
                    chain=chain.code,
                    sender=evm_task.sender.address,
                    nonce=evm_task.nonce,
                    error=str(status),
                )
                continue

            if status == TxCheckStatus.SUCCEEDED:
                assert tx_hash is not None  # SUCCEEDED 分支一定携带命中的 hash
                assert receipt is not None
                if not cls._has_required_confirmations(
                    chain=evm_task.chain,
                    receipt=receipt,
                ):
                    continue
                try:
                    cls.process_succeeded_receipt(
                        evm_task=evm_task,
                        tx_hash=tx_hash,
                        receipt=receipt,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "轮询器处理成功 receipt 失败",
                        chain=chain.code,
                        sender=evm_task.sender.address,
                        nonce=evm_task.nonce,
                        tx_hash=tx_hash,
                    )
                    continue
            elif status == TxCheckStatus.FAILED:
                try:
                    cls.finalize_failed_task(evm_task=evm_task)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "轮询器收口失败交易异常",
                        chain=chain.code,
                        sender=evm_task.sender.address,
                        nonce=evm_task.nonce,
                    )
                    continue
            else:
                if evm_task.last_attempt_at >= ago(
                    seconds=EVM_PENDING_REBROADCAST_TIMEOUT
                ):
                    continue
                # 长时间所有历史 hash 都找不到 receipt，按 mempool 丢弃路径重新广播。
                try:
                    evm_task.broadcast(allow_pending_chain_rebroadcast=True)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "PENDING_CHAIN 超时重新执行失败",
                        chain=chain.code,
                        sender=evm_task.sender.address,
                        nonce=evm_task.nonce,
                    )
                else:
                    logger.info(
                        "PENDING_CHAIN 超时且无链上记录，已重新广播",
                        chain=chain.code,
                        sender=evm_task.sender.address,
                        nonce=evm_task.nonce,
                    )

    @staticmethod
    def _find_receipt_across_hashes(
        *, evm_task: EvmTxTask
    ) -> tuple[TxCheckStatus | Exception, str | None, dict | None]:
        """遍历任务的所有历史 tx_hash 查找链上 receipt。

        返回 (status, tx_hash, receipt):
        - 找到成功 receipt -> (SUCCEEDED, 命中的 hash, receipt)
        - 找到失败 receipt -> (FAILED, 命中的 hash, None)
        - 全部未找到 -> (MISSING, None, None)
        - RPC 异常 -> (Exception, None, None)
        """
        for tx_hash in evm_task.known_tx_hashes():
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
                return TxCheckStatus.SUCCEEDED, tx_hash, dict(receipt)
            if status == 0:
                return TxCheckStatus.FAILED, tx_hash, None
            return RuntimeError("EVM receipt status missing or invalid"), None, None

        return TxCheckStatus.MISSING, None, None

    @staticmethod
    def _has_required_confirmations(*, chain: Chain, receipt: dict) -> bool:
        block_number = receipt.get("blockNumber")
        if block_number is None:
            return False
        confirmed_at_or_before = chain.latest_block_number - chain.confirm_block_count
        return int(block_number) <= confirmed_at_or_before

    @staticmethod
    def process_succeeded_receipt(
        *,
        evm_task: EvmTxTask,
        tx_hash: str,
        receipt: dict,
    ) -> bool:
        """成功 receipt 达到确认数后，把交易交给内部处理器统一推进。"""
        from evm.internal_tx.processor import process_internal_transaction

        chain = evm_task.chain
        if not EvmTaskPoller._has_required_confirmations(
            chain=chain,
            receipt=receipt,
        ):
            return False
        tx = chain.w3.eth.get_transaction(tx_hash)
        return process_internal_transaction(chain=chain, tx=dict(tx), receipt=receipt)

    @staticmethod
    @db_transaction.atomic
    def finalize_failed_task(*, evm_task: EvmTxTask) -> bool:
        locked_task = EvmTxTask.objects.select_for_update().get(pk=evm_task.pk)

        base_task = locked_task.base_task
        if base_task.status != TxTaskStatus.PENDING_CHAIN:
            return False

        updated = TxTask.mark_finalized_failed(
            task_id=base_task.pk,
            expected_status=TxTaskStatus.PENDING_CHAIN,
        )
        if not updated:
            return False

        if base_task.tx_type == TxTaskType.VaultSlotDeploy:
            mark_deployed_if_on_chain_for_task(base_task)
        return True
