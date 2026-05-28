from __future__ import annotations

from dataclasses import dataclass

from chains.models import Chain
from chains.models import Transfer
from chains.models import TxTask
from evm.internal_tx.direct_transfer import match_direct_transfer_fact
from evm.internal_tx.facts import MatchedTransferFact


def withdrawal_matcher(
    *,
    chain: Chain,
    tx_task: TxTask,
    receipt: dict,
    tx: dict | None = None,
) -> MatchedTransferFact | None:
    """提取 Withdrawal 预期的资产移动事实。"""
    return match_direct_transfer_fact(
        chain=chain,
        tx_task=tx_task,
        receipt=receipt,
        tx=tx,
    )


@dataclass
class WithdrawalHandler:
    """提现业务生命周期 handler，把扫描事件转交给 WithdrawalService。"""

    def match(self, transfer: Transfer, tx_task: TxTask) -> bool:
        """关联 Transfer 与提现单。"""
        from withdrawals.service import WithdrawalService

        return WithdrawalService.try_match_withdrawal(transfer, tx_task)

    def confirm(self, transfer: Transfer) -> None:
        """达到确认数后推进提现状态。"""
        from withdrawals.service import WithdrawalService

        WithdrawalService.confirm_withdrawal(transfer)

    def drop(self, transfer: Transfer) -> None:
        """Transfer 因 reorg 被撤销时回退提现状态。"""
        from withdrawals.service import WithdrawalService

        WithdrawalService.drop_withdrawal(transfer)

    def finalize_failed(self, tx_task: TxTask) -> None:
        """链上 receipt 失败时，把提现单置为失败。"""
        from withdrawals.service import WithdrawalService

        WithdrawalService.fail_withdrawal(tx_task=tx_task)


withdrawal_handler = WithdrawalHandler()
