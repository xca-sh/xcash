from __future__ import annotations

from django.db.models.signals import post_save
from django.dispatch import receiver

from chains.models import DepositVaultSlot
from chains.models import InvoiceVaultSlot
from chains.models import VaultSlot
from common.saas_callback import CallbackEvent
from common.saas_callback import SaasCallback
from common.saas_callback import send_saas_callback


def vault_slot_open_sys_no(address: str) -> str:
    """收款地址占用费的幂等键：按地址而非 VaultSlot 行。

    EVM 各链共用同一 CREATE2 地址（salt 不掺链），同一地址在多条链各建一行也只算一次占用；
    salt 含 project_id，地址天然按项目隔离，不必再拼 appid。
    """
    return f"vault-slot-open:{address}"


def report_vault_slot_opened(slot: VaultSlot) -> None:
    """项目新增收款合约地址时上报 SaaS 扣占用费，在创建事务内入队、随之提交或回滚。

    同一项目里该地址已有其他链的 VaultSlot（跨链补建、商户在另一条 EVM 链取同一地址）
    说明已上报过，不再重复上报；并发建行漏判时由 SaaS 按 sys_no 去重兜底。
    """
    if (
        VaultSlot.objects.filter(project_id=slot.project_id, address=slot.address)
        .exclude(pk=slot.pk)
        .exists()
    ):
        return
    send_saas_callback(
        SaasCallback(
            event=CallbackEvent.VAULT_SLOT_CREATED,
            appid=slot.project.appid,
            sys_no=vault_slot_open_sys_no(slot.address),
            detail={
                "address": slot.address,
                "chain": slot.chain.code,
                "usage": slot.usage,
            },
        )
    )


# 挂在 post_save 而非各创建函数里：充值 / 账单分配、扫描器跨链补建以及以后新增的任何
# 创建路径都会经过这里，不会漏收。代理模型保存时 sender 是代理类，需要一并注册。
@receiver(post_save, sender=VaultSlot)
@receiver(post_save, sender=DepositVaultSlot)
@receiver(post_save, sender=InvoiceVaultSlot)
def report_vault_slot_opened_on_create(
    sender, *, instance: VaultSlot, created: bool, raw: bool = False, **kwargs
) -> None:
    # raw=True 是 loaddata 导入 fixture，不代表真实开户
    if not created or raw:
        return
    report_vault_slot_opened(instance)
