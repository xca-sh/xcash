from __future__ import annotations

from dataclasses import dataclass

from chains.models import BroadcastTask
from chains.models import BroadcastTaskFailureReason
from chains.models import Chain
from chains.models import OnchainTransfer
from evm.internal_tx.direct_transfer import match_direct_transfer_fact
from evm.internal_tx.facts import MatchedTransferFact


def withdrawal_matcher(
    *,
    chain: Chain,
    broadcast_task: BroadcastTask,
    receipt: dict,
) -> MatchedTransferFact | None:
    """提取 Withdrawal 预期的资产移动事实。"""
    return match_direct_transfer_fact(
        chain=chain,
        broadcast_task=broadcast_task,
        receipt=receipt,
    )


@dataclass
class WithdrawalHandler:
    def match(self, transfer: OnchainTransfer, broadcast_task: BroadcastTask) -> bool:
        from withdrawals.service import WithdrawalService

        return WithdrawalService.try_match_withdrawal(transfer, broadcast_task)

    def confirm(self, transfer: OnchainTransfer) -> None:
        from withdrawals.service import WithdrawalService

        WithdrawalService.confirm_withdrawal(transfer)

    def drop(self, transfer: OnchainTransfer) -> None:
        from withdrawals.service import WithdrawalService

        WithdrawalService.drop_withdrawal(transfer)

    def finalize_failed(
        self,
        broadcast_task: BroadcastTask,
        reason: BroadcastTaskFailureReason,
    ) -> None:
        from withdrawals.service import WithdrawalService

        WithdrawalService.fail_withdrawal(broadcast_task=broadcast_task)


withdrawal_handler = WithdrawalHandler()
