from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction

from chains.models import Address
from chains.models import ChainType
from evm.intents import Eip3009Authorization
from evm.intents import build_x402_eip3009_facilitate_intent
from evm.models import EvmBroadcastTask
from evm.models import X402Facilitation
from evm.models import X402FacilitationStatus


@dataclass
class X402CreateResult:
    facilitation: X402Facilitation


class X402FacilitationService:
    @classmethod
    @db_transaction.atomic
    def create_and_schedule(
        cls,
        *,
        facilitator: Address,
        chain,
        crypto,
        authorization: Eip3009Authorization,
    ) -> X402CreateResult:
        if facilitator.chain_type != ChainType.EVM:
            raise ValidationError("facilitator must be EVM system address")

        facilitation = X402Facilitation.objects.create(
            chain=chain,
            crypto=crypto,
            facilitator_address=facilitator,
            authorization_from_address=authorization.from_address,
            authorization_to_address=authorization.to,
            authorization_value_raw=authorization.value,
            valid_after=authorization.valid_after,
            valid_before=authorization.valid_before,
            authorization_nonce=authorization.nonce,
            authorization_v=authorization.v,
            authorization_r=authorization.r,
            authorization_s=authorization.s,
            status=X402FacilitationStatus.CREATED,
        )

        intent = build_x402_eip3009_facilitate_intent(
            address=facilitator,
            chain=chain,
            crypto=crypto,
            authorization=authorization,
        )
        evm_task = EvmBroadcastTask.schedule(intent)

        facilitation.broadcast_task = evm_task.base_task
        facilitation.status = X402FacilitationStatus.BROADCASTED
        facilitation.save(update_fields=["broadcast_task", "status", "updated_at"])

        return X402CreateResult(facilitation=facilitation)

