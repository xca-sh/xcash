from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction
from web3 import Web3

from chains.models import Address
from chains.models import Chain
from chains.models import ChainType
from evm.contracts_codec import build_collector_init_code
from evm.intents import build_payment_collector_deploy_intent
from evm.intents import compute_create2_address
from evm.models import ContractDeployCollection
from evm.models import ContractDeployCollectionStatus
from evm.models import EvmBroadcastTask

if TYPE_CHECKING:
    from currencies.models import Crypto


@dataclass
class ContractDeployCollectionCreateResult:
    collection: ContractDeployCollection


class ContractDeployCollectionService:
    @classmethod
    @db_transaction.atomic
    def create_and_schedule(
        cls,
        *,
        deployer: Address,
        chain: Chain,
        crypto: Crypto,
        salt: bytes,
        vault_address: str,
        expected_collect_value_raw: int,
        gas: int,
    ) -> ContractDeployCollectionCreateResult:
        if deployer.chain_type != ChainType.EVM:
            raise ValidationError("deployer 必须是 EVM 系统地址")
        if not chain.create2_factory_address:
            raise ValueError(f"Chain {chain.code} 未配置 create2_factory_address")
        if expected_collect_value_raw < 0:
            raise ValueError("expected_collect_value_raw 必须 >= 0")
        if not crypto.support_this_chain(chain):
            raise ValueError(f"Crypto {crypto.symbol} is not deployed on chain {chain.code}")

        factory_address = Web3.to_checksum_address(chain.create2_factory_address)
        vault_checksum = Web3.to_checksum_address(vault_address)
        token_address = crypto.address(chain) or None
        collector_init_code = build_collector_init_code(
            to=vault_checksum,
            token=token_address,
        )
        collector_init_code_hash = Web3.keccak(collector_init_code)
        collector_address = compute_create2_address(
            factory_address=factory_address,
            salt=salt,
            init_code_hash=collector_init_code_hash,
        )

        collection = ContractDeployCollection.objects.create(
            chain=chain,
            crypto=crypto,
            deployer_address=deployer,
            factory_address=factory_address,
            collector_address=collector_address,
            vault_address=vault_checksum,
            salt=salt,
            collector_init_code_hash=collector_init_code_hash,
            expected_collect_value_raw=expected_collect_value_raw,
            status=ContractDeployCollectionStatus.CREATED,
        )

        intent = build_payment_collector_deploy_intent(
            address=deployer,
            chain=chain,
            salt=salt,
            collector_init_code=collector_init_code,
            gas=gas,
        )
        evm_task = EvmBroadcastTask.schedule(intent)

        collection.broadcast_task = evm_task.base_task
        collection.status = ContractDeployCollectionStatus.BROADCASTED
        collection.save(update_fields=["broadcast_task", "status", "updated_at"])

        return ContractDeployCollectionCreateResult(collection=collection)
