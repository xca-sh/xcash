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


def validate_runtime(*, chain: Chain) -> None:
    if chain.type != ChainType.TRON:
        raise ValueError("Tron VaultSlot 仅支持 Tron 链")
    if (
        not settings.TRON_VAULT_SLOT_FACTORY_ADDRESS
        or not settings.TRON_VAULT_SLOT_TEMPLATE_ADDRESS
    ):
        raise RuntimeError("Tron VaultSlot factory/template 未配置")


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


def can_create_collect_tx_task(*, chain: Chain, slot: VaultSlot) -> bool:
    if slot.is_deployed:
        return True
    # TVM 对无 code 地址的合约调用会返回 success 但什么都不做；未部署则跳过本轮，
    # 等部署确认后下一轮再建，避免把资金仍滞留误判为归集成功。
    if not TronAdapter().is_contract(chain, slot.address):
        return False
    VaultSlot.objects.filter(pk=slot.pk, is_deployed=False).update(is_deployed=True)
    slot.is_deployed = True
    return True
