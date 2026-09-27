"""Tron 链族向 chains 引擎登记的实现，由 TronConfig.ready() 调用 register()。"""

from django.db.models.signals import post_save
from tron import vault_slots
from tron.adapter import TronAdapter
from tron.models import TronWatchCursor

from chains.constants import ChainType
from chains.models import Chain
from chains.registry import ChainFamily
from chains.registry import register_chain_family


def ensure_tron_scan_cursor(sender, instance: Chain, **kwargs) -> None:
    """活跃 Tron 链在配置层即持有按链唯一的扫描游标，避免依赖首次 beat 扫描显式创建。

    游标只锚定区块进度、与具体资产解耦：扫描器每轮按本链全量 CryptoOnChain
    逐块拉取，新增/下架 TRC20 或原生 TRX 不影响游标，故无需依赖 USDT 是否已配置。
    Chain.save() 把 super().save() 包在事务里，post_save 在同一事务内执行，
    游标与链配置同生同灭。fixture 原样导入（raw）时不做联动。
    """
    if kwargs.get("raw") or instance.type != ChainType.TRON or not instance.active:
        return
    TronWatchCursor.objects.get_or_create(
        chain=instance,
        defaults={"last_scanned_block": 0, "enabled": True},
    )


def register() -> None:
    register_chain_family(
        ChainFamily(
            chain_type=ChainType.TRON,
            adapter_class=TronAdapter,
            vault_slot_backend=vault_slots,
        )
    )
    post_save.connect(
        ensure_tron_scan_cursor,
        sender=Chain,
        dispatch_uid="tron.ensure_tron_scan_cursor",
    )
