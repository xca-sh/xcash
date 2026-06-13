from __future__ import annotations

from tron.adapter import TronAdapter
from tron.contracts_codec import predict_tron_vault_slot_address
from tron.intents import build_vault_slot_collect_intent
from tron.intents import build_vault_slot_deploy_intent
from tron.models import TronTxTask

from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from chains.models import TxTask
from chains.models import VaultSlot
from core.models import SystemWallet

# 原生币在 CryptoOnChain 里 address=""，但归集要调 collect(address(0))；这里把空地址
# 映射成 EVM 零地址，build_*_intent 会按 address(0) ABI 编码，命中 VaultSlot 原生币清扫分支。
NATIVE_COLLECT_TOKEN_ADDRESS = "0x0000000000000000000000000000000000000000"


def collect_token_address(*, crypto, chain: Chain) -> str:
    """归集时该 crypto 对应的 token 入参：原生币用 address(0)，TRC20 用其合约地址。"""
    if crypto.is_native:
        return NATIVE_COLLECT_TOKEN_ADDRESS
    return crypto.address(chain)


def predict_address(*, chain: Chain, vault: str, salt: bytes) -> str:
    addresses = chain.vault_slot_contract_addresses()
    return predict_tron_vault_slot_address(
        vault=vault,
        salt=salt,
        factory=addresses.factory,
        vault_slot_implementation=addresses.implementation,
    )


def is_deployed_on_chain(*, chain: Chain, address: str) -> bool:
    return TronAdapter().is_contract(chain, address)


def create_deploy_tx_task(*, slot: VaultSlot) -> TxTask:
    sender = SystemWallet.get_current().wallet.get_address(
        chain_type=ChainType.TRON,
        usage=AddressUsage.HotWallet,
    )
    addresses = slot.chain.vault_slot_contract_addresses()
    intent = build_vault_slot_deploy_intent(
        sender=sender,
        chain=slot.chain,
        factory_address=addresses.factory,
        vault_address=slot.project.tron_vault,
        salt=bytes(slot.salt),
    )
    return TronTxTask.schedule(intent).base_task


def create_collect_tx_task(*, chain: Chain, crypto, slot: VaultSlot) -> TxTask:
    # 归集前置闸门保证只有已部署的 slot 走到这里;未部署一律先走部署任务。
    if not slot.is_deployed:
        raise RuntimeError(f"VaultSlot {slot.pk} 尚未部署,不能创建归集任务")
    sender = SystemWallet.get_current().wallet.get_address(
        chain_type=ChainType.TRON,
        usage=AddressUsage.HotWallet,
    )
    intent = build_vault_slot_collect_intent(
        sender=sender,
        chain=chain,
        slot_address=slot.address,
        token_address=collect_token_address(crypto=crypto, chain=chain),
    )
    # 每个到期计划各建一笔独立任务；collect 是按当前余额全额清扫的幂等操作，
    # 余额为 0 时模板直接 return，不会重复归集。
    return TronTxTask.schedule(intent).base_task
