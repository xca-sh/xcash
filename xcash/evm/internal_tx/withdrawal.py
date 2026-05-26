from __future__ import annotations

from dataclasses import dataclass

from chains.models import Chain
from chains.models import Transfer
from chains.models import TxTask
from evm.internal_tx.direct_transfer import match_direct_transfer_fact
from evm.internal_tx.routing import MatchedTransferFact


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
    def match(self, transfer: Transfer, tx_task: TxTask) -> bool:
        from withdrawals.service import WithdrawalService

        return WithdrawalService.try_match_withdrawal(transfer, tx_task)

    def confirm(self, transfer: Transfer) -> None:
        from withdrawals.service import WithdrawalService

        WithdrawalService.confirm_withdrawal(transfer)

    def drop(self, transfer: Transfer) -> None:
        from withdrawals.service import WithdrawalService

        WithdrawalService.drop_withdrawal(transfer)

    def finalize_failed(self, tx_task: TxTask) -> None:
        from withdrawals.service import WithdrawalService

        WithdrawalService.fail_withdrawal(tx_task=tx_task)


withdrawal_handler = WithdrawalHandler()
