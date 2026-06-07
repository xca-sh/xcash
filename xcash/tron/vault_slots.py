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
        vault_address=slot.project.vault,
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
            token_address=crypto.address(chain),
        )
    else:
        intent = build_vault_slot_ensure_collect_intent(
            sender=sender,
            chain=chain,
            factory_address=settings.TRON_VAULT_SLOT_FACTORY_ADDRESS,
            vault_address=slot.project.vault,
            salt=bytes(slot.salt),
            token_address=crypto.address(chain),
        )
    # 每个到期计划各建一笔独立任务；collect 是按当前余额全额清扫的幂等操作，
    # 余额为 0 时模板直接 return，不会重复归集。
    return TronTxTask.schedule(intent).base_task


def can_create_collect_tx_task(*, chain: Chain, crypto, slot: VaultSlot) -> bool:
    if slot.is_deployed:
        return True
    if not slot.project.vault:
        return False
    if getattr(crypto, "pk", None) != chain.native_coin.pk:
        return True
    # 原生币必须先有 receive() 合约才能被系统观测；若标记未部署，则先按链上 code
    # 事实复检，不能向无 code 地址发 collect。
    if not TronAdapter().is_contract(chain, slot.address):
        return False
    VaultSlot.objects.filter(pk=slot.pk, is_deployed=False).update(is_deployed=True)
    slot.is_deployed = True
    return True
