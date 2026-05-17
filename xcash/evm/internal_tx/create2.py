from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from chains.models import (
    BroadcastTask,
    BroadcastTaskFailureReason,
    OnchainActionType,
    OnchainTransfer,
)
from django.db import transaction as db_transaction
from django.utils import timezone
from evm.internal_tx._log_utils import matches_transfer_log, normalize_log_index
from evm.internal_tx.facts import MatchedTransferFact


def create2_matcher(*, chain, broadcast_task: BroadcastTask, receipt: dict):
    from evm.models import ContractDeployCollection

    try:
        collection = broadcast_task.contract_deploy_collection
    except ContractDeployCollection.DoesNotExist:
        return None

    decimals = collection.crypto.get_decimals(chain)
    expected_value = Decimal(collection.expected_collect_value_raw)
    matches = [
        log
        for log in receipt.get("logs") or []
        if matches_transfer_log(
            log,
            token=collection.crypto.address(chain),
            from_address=collection.collector_address,
            to_address=collection.vault_address,
            value=expected_value,
        )
    ]
    if len(matches) != 1:
        return None
    log = matches[0]
    return MatchedTransferFact(
        event_id=f"erc20:{normalize_log_index(log['logIndex'])}",
        from_address=collection.collector_address,
        to_address=collection.vault_address,
        crypto=collection.crypto,
        value=expected_value,
        amount=expected_value.scaleb(-decimals),
    )


@dataclass
class ContractDeployCollectionHandler:
    def match(self, transfer: OnchainTransfer, broadcast_task: BroadcastTask) -> bool:
        from evm.models import ContractDeployCollection

        with db_transaction.atomic():
            collection = (
                ContractDeployCollection.objects.select_for_update()
                .filter(broadcast_task=broadcast_task)
                .first()
            )
            if collection is None:
                return False
            if collection.transfer_id and collection.transfer_id != transfer.pk:
                return False
            collection.transfer = transfer
            collection.save(update_fields=["transfer", "updated_at"])
            transfer.type = OnchainActionType.ContractDeployCollect
            transfer.save(update_fields=["type"])
        return True

    def confirm(self, transfer: OnchainTransfer) -> None:
        from evm.models import ContractDeployCollection, ContractDeployCollectionStatus

        ContractDeployCollection.objects.filter(transfer=transfer).update(
            status=ContractDeployCollectionStatus.CONFIRMED,
            updated_at=timezone.now(),
        )

    def drop(self, transfer: OnchainTransfer) -> None:
        from evm.models import ContractDeployCollection, ContractDeployCollectionStatus

        ContractDeployCollection.objects.filter(transfer=transfer).update(
            transfer=None,
            status=ContractDeployCollectionStatus.DROPPED,
            updated_at=timezone.now(),
        )

    def finalize_failed(
        self,
        broadcast_task: BroadcastTask,
        reason: BroadcastTaskFailureReason,
    ) -> None:
        from evm.models import ContractDeployCollection, ContractDeployCollectionStatus

        ContractDeployCollection.objects.filter(broadcast_task=broadcast_task).update(
            status=ContractDeployCollectionStatus.FAILED,
            failure_reason=reason,
            updated_at=timezone.now(),
        )


contract_deploy_collection_handler = ContractDeployCollectionHandler()
