from __future__ import annotations

from django.conf import settings
from tron.adapter import TronAdapter
from tron.contracts_codec import predict_tron_vault_slot_address
from tron.intents import build_vault_slot_collect_intent
from tron.intents import build_vault_slot_deploy_intent
from tron.intents import build_vault_slot_ensure_collect_intent
from tron.models import TronTxTask

from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from chains.models import TxTask
from chains.models import VaultSlot
from core.models import SystemWallet

# 原生币在 CryptoOnChain 里 address=""，但归集要调 collect(address(0))；这里把空地址
# 映射成 EVM 零地址，build_*_intent 会按 address(0) ABI 编码，命中模板的原生币清扫分支。
NATIVE_COLLECT_TOKEN_ADDRESS = "0x0000000000000000000000000000000000000000"


def collect_token_address(*, crypto, chain: Chain) -> str:
    """归集时该 crypto 对应的 token 入参：原生币用 address(0)，TRC20 用其合约地址。"""
    if crypto.is_native:
        return NATIVE_COLLECT_TOKEN_ADDRESS
    return crypto.address(chain)


def predict_address(*, vault: str, salt: bytes) -> str:
    return predict_tron_vault_slot_address(
        vault=vault,
        salt=salt,
        factory=settings.TRON_VAULT_SLOT_FACTORY_ADDRESS,
        vault_slot_template=settings.TRON_VAULT_SLOT_TEMPLATE_ADDRESS,
    )


def is_deployed_on_chain(*, chain: Chain, address: str) -> bool:
    return TronAdapter().is_contract(chain, address)


def create_deploy_tx_task(*, slot: VaultSlot) -> TxTask:
    sender = SystemWallet.get_current().wallet.get_address(
        chain_type=ChainType.TRON,
        usage=AddressUsage.HotWallet,
    )
    intent = build_vault_slot_deploy_intent(
        sender=sender,
        chain=slot.chain,
        factory_address=settings.TRON_VAULT_SLOT_FACTORY_ADDRESS,
        vault_address=slot.project.tron_vault,
        salt=bytes(slot.salt),
    )
    return TronTxTask.schedule(intent).base_task


def create_collect_tx_task(*, chain: Chain, crypto, slot: VaultSlot) -> TxTask:
    sender = SystemWallet.get_current().wallet.get_address(
        chain_type=ChainType.TRON,
        usage=AddressUsage.HotWallet,
    )
    if slot.is_deployed:
        intent = build_vault_slot_collect_intent(
            sender=sender,
            chain=chain,
            slot_address=slot.address,
            token_address=collect_token_address(crypto=crypto, chain=chain),
        )
    else:
        intent = build_vault_slot_ensure_collect_intent(
            sender=sender,
            chain=chain,
            factory_address=settings.TRON_VAULT_SLOT_FACTORY_ADDRESS,
            vault_address=slot.project.tron_vault,
            salt=bytes(slot.salt),
            token_address=collect_token_address(crypto=crypto, chain=chain),
        )
    # 每个到期计划各建一笔独立任务；collect 是按当前余额全额清扫的幂等操作，
    # 余额为 0 时模板直接 return，不会重复归集。
    return TronTxTask.schedule(intent).base_task


def can_create_collect_tx_task(*, chain: Chain, crypto, slot: VaultSlot) -> bool:
    if slot.is_deployed:
        return True
    # 决策 B：未部署也可归集。归集走 factory 的 ensureDeployedAndCollect，一笔交易完成
    # 部署 + 清扫（原生币传 address(0)，命中模板原生分支）。Tron 上 TransferContract 不触发
    # receive()，原生入账靠区块扫描观测、与 slot 是否预先部署无关，故原生币不再要求先存在
    # receive() 合约，与 TRC20 共用同一条未部署路径。唯一前提是已知 vault，否则无法预测部署 slot。
    return bool(slot.project.tron_vault)
