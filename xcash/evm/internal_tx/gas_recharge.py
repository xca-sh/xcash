from __future__ import annotations

from dataclasses import dataclass

from chains.models import (
    BroadcastTask,
    BroadcastTaskFailureReason,
    Chain,
    OnchainTransfer,
)
from django.utils import timezone
from evm.internal_tx.direct_transfer import match_direct_transfer_fact
from evm.internal_tx.facts import MatchedTransferFact


def gas_recharge_matcher(
    *,
    chain: Chain,
    broadcast_task: BroadcastTask,
    receipt: dict,
) -> MatchedTransferFact | None:
    return match_direct_transfer_fact(
        chain=chain,
        broadcast_task=broadcast_task,
        receipt=receipt,
    )


@dataclass
class GasRechargeHandler:
    def match(self, transfer: OnchainTransfer, broadcast_task: BroadcastTask) -> bool:
        from deposits.service import DepositService

        return DepositService.try_match_gas_recharge(transfer, broadcast_task)

    def confirm(self, transfer: OnchainTransfer) -> None:
        from deposits.models import GasRecharge

        GasRecharge.objects.filter(transfer=transfer).update(recharged_at=timezone.now())

    def drop(self, transfer: OnchainTransfer) -> None:
        return None

    def finalize_failed(
        self,
        broadcast_task: BroadcastTask,
        reason: BroadcastTaskFailureReason,
    ) -> None:
        return None


gas_recharge_handler = GasRechargeHandler()
