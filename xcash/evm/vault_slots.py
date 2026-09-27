from __future__ import annotations

import structlog
from web3 import Web3

from chains.constants import VaultSlotContractAddresses
from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from chains.models import TxTask
from chains.models import VaultSlot
from core.models import SystemWallet
from evm.adapter import EvmAdapter
from evm.constants import DEFAULT_VAULT_SLOT_COLLECT_GAS
from evm.constants import VAULT_SLOT_COLLECT_GAS_CEILING
from evm.constants import VAULT_SLOT_COLLECT_GAS_ESTIMATE_BUFFER_BPS
from evm.constants import XCASH_VAULT_SLOT_FACTORY_ADDRESS
from evm.constants import XCASH_VAULT_SLOT_IMPLEMENTATION_ADDRESS
from evm.contracts_codec import predict_xcash_vault_slot_address
from evm.intents import build_vault_slot_collect_intent
from evm.intents import build_vault_slot_deploy_intent
from evm.models import EvmTxTask

logger = structlog.get_logger()

BPS_DENOMINATOR = 10_000

# 原生币在 CryptoOnChain 里 address=""，但 VaultSlot 用 collect(address(0))
# 表示清扫合约当前原生币余额。这里必须按「本链 native_coin」判断，避免把其它链
# 的原生币 Crypto 误映射成 address(0)。
NATIVE_COLLECT_TOKEN_ADDRESS = "0x0000000000000000000000000000000000000000"


def collect_token_address(*, crypto, chain: Chain) -> str:
    """归集时该 crypto 对应的 token 入参：本链原生币用 address(0)，ERC20 用合约地址。"""
    if getattr(crypto, "pk", None) == chain.native_coin.pk:
        return NATIVE_COLLECT_TOKEN_ADDRESS
    return crypto.address(chain)


def contract_addresses(chain: Chain) -> VaultSlotContractAddresses:
    """EVM 通过确定性 CREATE2 在所有 EVM 链上共享同一组 VaultSlot 基础合约地址。"""
    return VaultSlotContractAddresses(
        factory=XCASH_VAULT_SLOT_FACTORY_ADDRESS,
        implementation=XCASH_VAULT_SLOT_IMPLEMENTATION_ADDRESS,
    )


def predict_address(*, chain: Chain, vault: str, salt: bytes) -> str:
    addresses = chain.vault_slot_contract_addresses()
    return predict_xcash_vault_slot_address(
        vault=vault,
        salt=salt,
        factory=addresses.factory,
        vault_slot_implementation=addresses.implementation,
    )


def is_deployed_on_chain(*, chain: Chain, address: str) -> bool:
    return EvmAdapter.is_contract(chain, address)


def create_deploy_tx_task(*, slot: VaultSlot) -> TxTask:
    sender = SystemWallet.get_current().wallet.get_address(
        chain_type=ChainType.EVM,
        usage=AddressUsage.HotWallet,
    )
    addresses = slot.chain.vault_slot_contract_addresses()
    intent = build_vault_slot_deploy_intent(
        sender=sender,
        chain=slot.chain,
        factory_address=addresses.factory,
        vault_address=Web3.to_checksum_address(slot.project.evm_vault),
        salt=bytes(slot.salt),
    )
    return EvmTxTask.schedule(intent).base_task


def estimate_collect_gas(*, chain: Chain, crypto, slot: VaultSlot) -> int | None:
    """按链上 estimate_gas 估算归集 gas 并上浮；无法估算/失败时返回 None（用静态默认）。

    必须在调度短事务与热钱包 nonce 锁之外调用（execute_one_due 的 RPC 前置区），
    避免把 estimate RPC 关进 DB 事务或全局热钱包锁。任何异常都吞掉回退静态默认，
    绝不因估算失败阻断归集本身。
    """
    if chain.type != ChainType.EVM or not slot.is_deployed:
        return None
    try:
        sender = SystemWallet.get_current().wallet.get_address(
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
        )
        intent = build_vault_slot_collect_intent(
            sender=sender,
            chain=chain,
            slot_address=slot.address,
            token_address=collect_token_address(crypto=crypto, chain=chain),
        )
        estimated = int(
            chain.w3.eth.estimate_gas(  # noqa: SLF001
                {
                    "from": sender.address,
                    "to": intent.to,
                    "data": intent.data,
                    "value": 0,
                }
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "EVM 归集 gas 链上估算失败，回退静态默认",
            chain=chain.code,
            vault_slot_id=slot.pk,
            error=str(exc),
        )
        return None

    buffered = estimated * VAULT_SLOT_COLLECT_GAS_ESTIMATE_BUFFER_BPS // BPS_DENOMINATOR
    # max(静态默认) 防止低估回退（L1 常规链 estimate 低于默认时仍用默认）；
    # 上限防止异常报价把 gas limit 放大到烧穿热钱包 gas 预检。
    return min(
        max(DEFAULT_VAULT_SLOT_COLLECT_GAS, buffered), VAULT_SLOT_COLLECT_GAS_CEILING
    )


def create_collect_tx_task(
    *, chain: Chain, crypto, slot: VaultSlot, collect_gas_hint: int | None = None
) -> TxTask:
    # 归集前置闸门保证只有已部署的 slot 走到这里;未部署一律先走部署任务。
    if not slot.is_deployed:
        raise RuntimeError(f"VaultSlot {slot.pk} 尚未部署,不能创建归集任务")
    # 归集交易只把 VaultSlot 内的资金转给合约写死的 vault，collect() 无权限校验，
    # 调用方仅承担 gas。故与部署一样统一用系统热钱包，全局只需维护一个热钱包的 gas。
    sender = SystemWallet.get_current().wallet.get_address(
        chain_type=ChainType.EVM,
        usage=AddressUsage.HotWallet,
    )
    intent = build_vault_slot_collect_intent(
        sender=sender,
        chain=chain,
        slot_address=slot.address,
        token_address=collect_token_address(crypto=crypto, chain=chain),
        gas=collect_gas_hint,
    )
    # 不复用在途归集任务:归集计划 tx_task 是 OneToOne,复用同一任务会让第二个
    # 窗口撞唯一约束;collect(token) 是全额清扫,独立任务最多产生余额为 0 的空扫。
    return EvmTxTask.schedule(intent).base_task
