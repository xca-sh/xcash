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
from evm.hexutil import hex_lower
from evm.models import EvmTxTask

logger = structlog.get_logger()


class EvmTaskPoller:
    """轮询内部 EVM 任务的链上终局状态。

    对 SUBMITTED 超过短轮询延迟仍未终局的任务，遍历所有历史 tx_hash 查询 receipt：
    - 查到 receipt (status=1) 且确认数达标 -> 交给内部交易处理器按 TxTask 收口
    - 查到 receipt (status=0) 且确认数达标 -> 标记失败终局
    - 所有 hash 均无 receipt 且超过重播阈值 -> 交易可能已被 mempool 丢弃，重新广播

    失败终局与成功终局一样要求确认数达标：status=0 的 receipt 在 reorg 后仍可能
    被回滚、该 nonce 未被真正消费；若 0 确认即终局，reorg 丢弃该交易后本系统既
    不会重发（schedule 恒取 max+1、FAILED 行占据该 nonce），更高 nonce 的已签任务
    又永远排在缺口之后无法上链，整条 (sender, chain) 队列永久停摆。
    """

    @classmethod
    def poll_chain(cls, *, chain: Chain) -> None:
        queryset = (
            EvmTxTask.objects.select_related("base_task", "sender")
            .filter(
                chain=chain,
                base_task__status=TxTaskStatus.SUBMITTED,
                last_attempt_at__lt=ago(seconds=EVM_PENDING_RECEIPT_POLL_DELAY),
            )
            .order_by("sender_id", "nonce", "created_at")
        )

        for evm_task in queryset:
            # 复用本轮已加载的链：否则每条任务首次访问 evm_task.chain 都要多查一次库、
            # 拿到新的 Chain 实例。确认数按本轮开始时的链高计算，只会偏保守、不会提前终局。
            evm_task.chain = chain
            status, tx_hash, receipt, _ = cls._find_receipt_across_hashes(
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
                assert receipt is not None  # FAILED 分支一定携带命中的 receipt
                # 与成功路径对称：失败也必须等确认数达标才终局，避免 reorg 回滚后
                # 该 nonce 未消费却被永久标记失败，卡死整条发送地址队列。
                if not cls._has_required_confirmations(
                    chain=evm_task.chain,
                    receipt=receipt,
                ):
                    continue
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
                    evm_task.rebroadcast_submitted()
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "SUBMITTED 超时重新执行失败",
                        chain=chain.code,
                        sender=evm_task.sender.address,
                        nonce=evm_task.nonce,
                    )
                else:
                    logger.info(
                        "SUBMITTED 超时且无链上记录，已重新广播",
                        chain=chain.code,
                        sender=evm_task.sender.address,
                        nonce=evm_task.nonce,
                    )

    @staticmethod
    def _find_receipt_across_hashes(
        *, evm_task: EvmTxTask
    ) -> tuple[TxCheckStatus | Exception, str | None, dict | None, bool]:
        """遍历任务的所有历史 tx_hash 查找链上 receipt。

        已达确认深度的 receipt 必须同时位于 canonical block；孤块 receipt 按未命中
        继续查其他历史 hash。未达确认深度时不额外查询 block，避免轮询期无谓 RPC。

        最后一位表示本轮是否观察到过 receipt，供 QUEUED 恢复路径决定是否先转为
        SUBMITTED；即使唯一 receipt 已不在 canonical chain，也不能把该任务当成从未
        广播过而继续走普通首播路径。
        """
        unconfirmed: tuple[TxCheckStatus, str, dict] | None = None
        receipt_observed = False
        for tx_hash in evm_task.known_tx_hashes():
            try:
                receipt = evm_task.chain.w3.eth.get_transaction_receipt(tx_hash)
            except TransactionNotFound:
                continue
            except Exception as exc:  # noqa: BLE001
                return exc, None, None, receipt_observed

            if receipt is None:
                continue
            receipt_observed = True
            normalized_receipt = dict(receipt)

            status = receipt.get("status")
            if status == 1:
                check_status = TxCheckStatus.SUCCEEDED
            elif status == 0:
                check_status = TxCheckStatus.FAILED
            else:
                return (
                    RuntimeError("EVM receipt status missing or invalid"),
                    None,
                    None,
                    receipt_observed,
                )

            if not EvmTaskPoller._has_required_confirmations(
                chain=evm_task.chain,
                receipt=normalized_receipt,
            ):
                unconfirmed = unconfirmed or (
                    check_status,
                    tx_hash,
                    normalized_receipt,
                )
                continue

            try:
                is_canonical = EvmTaskPoller.receipt_is_canonical(
                    chain=evm_task.chain,
                    receipt=normalized_receipt,
                )
            except Exception as exc:  # noqa: BLE001
                return exc, None, None, receipt_observed
            if not is_canonical:
                logger.warning(
                    "EVM 主动交易 receipt 已脱离 canonical chain，继续检查历史 hash",
                    chain=evm_task.chain.code,
                    tx_hash=tx_hash,
                    block_number=normalized_receipt.get("blockNumber"),
                    receipt_block_hash=normalized_receipt.get("blockHash"),
                )
                continue
            return check_status, tx_hash, normalized_receipt, receipt_observed

        if unconfirmed is not None:
            status, tx_hash, receipt = unconfirmed
            return status, tx_hash, receipt, receipt_observed
        return TxCheckStatus.MISSING, None, None, receipt_observed

    @staticmethod
    def receipt_is_canonical(*, chain: Chain, receipt: dict) -> bool:
        """确认 receipt.blockHash 仍是该高度的 canonical block hash。"""
        block_number = receipt.get("blockNumber")
        receipt_block_hash = receipt.get("blockHash")
        if block_number is None or receipt_block_hash is None:
            raise RuntimeError("EVM receipt blockNumber or blockHash missing")
        canonical_block = chain.w3.eth.get_block(int(block_number))
        canonical_block_hash = canonical_block.get("hash")
        if canonical_block_hash is None:
            raise RuntimeError("EVM canonical block hash missing")
        return hex_lower(receipt_block_hash) == hex_lower(canonical_block_hash)

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
        if base_task.status != TxTaskStatus.SUBMITTED:
            return False

        updated = TxTask.mark_finalized_failed(
            task_id=base_task.pk,
            expected_status=TxTaskStatus.SUBMITTED,
        )
        if not updated:
            return False

        if base_task.tx_type == TxTaskType.VaultSlotDeploy:
            mark_deployed_if_on_chain_for_task(base_task)
        logger.warning(
            "EVM 主动交易失败终局",
            evm_task_id=locked_task.pk,
            tx_task_id=base_task.pk,
            tx_type=base_task.tx_type,
            chain=locked_task.chain.code,
            sender=locked_task.sender.address,
            nonce=locked_task.nonce,
        )
        return True
