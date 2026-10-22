from __future__ import annotations

from tron.adapter import TronAdapter
from tron.contracts_codec import predict_tron_vault_slot_address
from tron.intents import build_vault_slot_collect_intent
from tron.intents import build_vault_slot_deploy_intent
from tron.models import TronTxTask

from chains.constants import TRON_VAULT_SLOT_CONTRACT_ADDRESSES
from chains.constants import VaultSlotContractAddresses
from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from chains.models import TxTask
from chains.models import VaultSlot
from chains.vault_slots import is_chain_native_crypto
from core.models import SystemWallet

# 原生币在 CryptoOnChain 里 address=""，但归集要调 collect(address(0))；这里把空地址
# 映射成 EVM 零地址，build_*_intent 会按 address(0) ABI 编码，命中 VaultSlot 原生币清扫分支。
NATIVE_COLLECT_TOKEN_ADDRESS = "0x0000000000000000000000000000000000000000"


def collect_token_address(*, crypto, chain: Chain) -> str:
    """归集时该 crypto 对应的 token 入参：本链原生币用 address(0)，TRC20 用其合约地址。

    必须按「本链原生币」判断而不是 Crypto.is_native：ETH/BNB 等以 TRC20 形式上架
    Tron 时 is_native 同样为真，误判会把代币归集变成 collect(address(0)) 扫 TRX，
    代币永远留在槽位里。
    """
    if is_chain_native_crypto(chain=chain, crypto=crypto):
        return NATIVE_COLLECT_TOKEN_ADDRESS
    return crypto.address(chain)


def contract_addresses(chain: Chain) -> VaultSlotContractAddresses:
    """Tron / TVM 的 VaultSlot 基础合约按网络独立部署，按链 code 取，避免主网与 Nile 混用。"""
    try:
        return TRON_VAULT_SLOT_CONTRACT_ADDRESSES[chain.code]
    except KeyError as exc:
        raise RuntimeError(f"链 {chain.code} 未登记 Tron VaultSlot 合约地址") from exc


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


def record_external_deploy(*, slot: VaultSlot, tx_hash: str) -> None:
    """Tron 无需补账：TRX 部署前入账由扫描器在链下观测，工厂事件也不带初始余额。"""


def estimate_collect_gas(*, chain: Chain, crypto, slot: VaultSlot) -> int | None:
    """Tron 归集不按 EVM gas 模型计费（走能量/带宽），无 gas limit 上浮需求。

    与 EVM 后端保持同名接口，供 chains 层统一按后端调用，这里恒返回 None。
    """
    return None


def create_collect_tx_task(
    *, chain: Chain, crypto, slot: VaultSlot, collect_gas_hint: int | None = None
) -> TxTask:
    # 归集前置闸门保证只有已部署的 slot 走到这里;未部署一律先走部署任务。
    # collect_gas_hint 仅 EVM 用于 gas limit 上浮，Tron 走能量/带宽模型，忽略即可。
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
