from __future__ import annotations

import contextlib
from dataclasses import dataclass

from chains.models import (
    BroadcastTask,
    BroadcastTaskFailureReason,
    Chain,
    OnchainTransfer,
)
from evm.internal_tx.direct_transfer import match_direct_transfer_fact
from evm.internal_tx.facts import MatchedTransferFact


def deposit_collection_matcher(
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
class DepositCollectionHandler:
    def match(self, transfer: OnchainTransfer, broadcast_task: BroadcastTask) -> bool:
        from deposits.service import DepositService

        return DepositService.try_match_collection(transfer, broadcast_task)

    def confirm(self, transfer: OnchainTransfer) -> None:
        from deposits.models import DepositCollection
        from deposits.service import DepositService

        with contextlib.suppress(DepositCollection.DoesNotExist):
            DepositService.confirm_collection(transfer.deposit_collection)

    def drop(self, transfer: OnchainTransfer) -> None:
        from deposits.models import DepositCollection
        from deposits.service import DepositService

        with contextlib.suppress(DepositCollection.DoesNotExist):
            DepositService.drop_collection(transfer.deposit_collection)

    def finalize_failed(
        self,
        broadcast_task: BroadcastTask,
        reason: BroadcastTaskFailureReason,
    ) -> None:
        from deposits.service import DepositService

        DepositService.release_failed_collection(broadcast_task=broadcast_task)


deposit_collection_handler = DepositCollectionHandler()
