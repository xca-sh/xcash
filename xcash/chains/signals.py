from __future__ import annotations

from django.db.models.signals import post_save
from django.dispatch import receiver

from chains.models import DepositVaultSlot
from chains.models import InvoiceVaultSlot
from chains.models import VaultSlot
from chains.models import VaultSlotUsage
from common.saas_callback import CallbackEvent
from common.saas_callback import SaasCallback
from common.saas_callback import send_saas_callback


def deposit_customer_open_sys_no(customer_id: int) -> str:
    """充值用户开通费的幂等键：每个充值用户（Customer）一次，与链、地址无关。

    用 Customer 主键而非 uid：uid 只在项目内唯一且最长 128 位，SaaS 幂等字段只有 64 位。
    """
    return f"deposit-customer-open:{customer_id}"


def report_deposit_customer_opened(slot: VaultSlot) -> None:
    """充值用户拿到第一个充币地址时上报 SaaS 扣开通费，在创建事务内入队、随之提交或回滚。

    按充值用户（uid）收一次：与商户的终端用户一一对应、由商户主动申请，费用可预期；
    同一用户之后在其他链（含 EVM 跨链补建、Tron 等不同地址）再开地址都不再收。
    账单（INVOICE）槽位是引擎按并发账单自动扩出的地址池，商户无法预知何时新增，不收费，
    数量由引擎系统参数封顶，部署 gas 仍走 gas_fee 实报实销。

    该用户已有其他 VaultSlot 说明已上报过，不再重复上报；并发建行漏判时由 SaaS 按 sys_no 去重兜底。
    """
    if slot.usage != VaultSlotUsage.DEPOSIT:
        return
    if VaultSlot.objects.filter(customer_id=slot.customer_id).exclude(pk=slot.pk).exists():
        return
    send_saas_callback(
        SaasCallback(
            event=CallbackEvent.VAULT_SLOT_CREATED,
            appid=slot.project.appid,
            sys_no=deposit_customer_open_sys_no(slot.customer_id),
            detail={
                "uid": slot.customer.uid,
                "address": slot.address,
                "chain": slot.chain.code,
            },
        )
    )


# 挂在 post_save 而非各创建函数里：充值地址分配、扫描器跨链补建以及以后新增的任何
# 创建路径都会经过这里，不会漏收（账单槽位在 report_deposit_customer_opened 内跳过）。代理模型保存时 sender 是代理类，需要一并注册。
@receiver(post_save, sender=VaultSlot)
@receiver(post_save, sender=DepositVaultSlot)
@receiver(post_save, sender=InvoiceVaultSlot)
def report_deposit_customer_opened_on_create(
    sender, *, instance: VaultSlot, created: bool, raw: bool = False, **kwargs
) -> None:
    # raw=True 是 loaddata 导入 fixture，不代表真实开户
    if not created or raw:
        return
    report_deposit_customer_opened(instance)
