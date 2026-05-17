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


def x402_matcher(*, chain, broadcast_task: BroadcastTask, receipt: dict):
    from evm.models import X402Facilitation

    try:
        facilitation = broadcast_task.x402_facilitation
    except X402Facilitation.DoesNotExist:
        return None

    decimals = facilitation.crypto.get_decimals(chain)
    expected_value = Decimal(facilitation.authorization_value_raw)
    matches = [
        log
        for log in receipt.get("logs") or []
        if matches_transfer_log(
            log,
            token=facilitation.crypto.address(chain),
            from_address=facilitation.authorization_from_address,
            to_address=facilitation.authorization_to_address,
            value=expected_value,
        )
    ]
    if len(matches) != 1:
        return None
    log = matches[0]
    return MatchedTransferFact(
        event_id=f"erc20:{normalize_log_index(log['logIndex'])}",
        from_address=facilitation.authorization_from_address,
        to_address=facilitation.authorization_to_address,
        crypto=facilitation.crypto,
        value=expected_value,
        amount=expected_value.scaleb(-decimals),
    )


@dataclass
class X402Handler:
    def match(self, transfer: OnchainTransfer, broadcast_task: BroadcastTask) -> bool:
        from evm.models import X402Facilitation

        with db_transaction.atomic():
            facilitation = (
                X402Facilitation.objects.select_for_update()
                .filter(broadcast_task=broadcast_task)
                .first()
            )
            if facilitation is None:
                return False
            if facilitation.transfer_id and facilitation.transfer_id != transfer.pk:
                return False
            facilitation.transfer = transfer
            facilitation.save(update_fields=["transfer", "updated_at"])
            transfer.type = OnchainActionType.X402Facilitate
            transfer.save(update_fields=["type"])
        return True

    def confirm(self, transfer: OnchainTransfer) -> None:
        from evm.models import X402Facilitation, X402FacilitationStatus

        X402Facilitation.objects.filter(transfer=transfer).update(
            status=X402FacilitationStatus.CONFIRMED,
            updated_at=timezone.now(),
        )

    def drop(self, transfer: OnchainTransfer) -> None:
        from evm.models import X402Facilitation, X402FacilitationStatus

        X402Facilitation.objects.filter(transfer=transfer).update(
            transfer=None,
            status=X402FacilitationStatus.DROPPED,
            updated_at=timezone.now(),
        )

    def finalize_failed(
        self,
        broadcast_task: BroadcastTask,
        reason: BroadcastTaskFailureReason,
    ) -> None:
        from evm.models import X402Facilitation, X402FacilitationStatus

        X402Facilitation.objects.filter(broadcast_task=broadcast_task).update(
            status=X402FacilitationStatus.FAILED,
            failure_reason=reason,
            updated_at=timezone.now(),
        )


x402_handler = X402Handler()
