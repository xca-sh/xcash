from django import forms
from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from unfold.decorators import display

from chains.models import Address
from chains.models import Chain
from chains.models import ChainType
from chains.models import Transfer
from chains.models import TxTask
from chains.models import Wallet
from common.admin import ModelAdmin
from common.admin import ReadOnlyModelAdmin

# Register your models here.


class ChainAdminForm(forms.ModelForm):
    class Meta:
        model = Chain
        fields = "__all__"  # noqa: DJ007


@admin.register(Chain)
class ChainAdmin(ModelAdmin):
    form = ChainAdminForm
    # 字段瘦身后，type / native_coin / confirm_block_count 已转为 property，
    # 通过 display 方法暴露到列表页，方便运维一眼看清链配置。
    list_display = (
        "chain",
        "type_display",
        "native_coin_display",
        "active",
        "confirm_block_count_display",
        "latest_block_number",
        "evm_log_max_block_range",
    )
    list_editable = (
        "active",
        "evm_log_max_block_range",
    )
    list_filter = ("active",)
    search_fields = ("chain",)

    @display(description=_("类型"))
    def type_display(self, obj: Chain) -> str:
        return obj.type

    @display(description=_("原生币"))
    def native_coin_display(self, obj: Chain) -> str:
        return obj.spec.native_coin_symbol

    @display(description=_("区块确认数"))
    def confirm_block_count_display(self, obj: Chain) -> int:
        return obj.confirm_block_count

    base_fieldsets = (
        (
            _("基本信息"),
            {
                "fields": (
                    "chain",
                    "active",
                )
            },
        ),
    )
    evm_fieldsets = (
        (
            "EVM",
            {
                "fields": (
                    "rpc",
                    "evm_log_max_block_range",
                )
            },
        ),
    )
    tron_fieldsets = (
        (
            "Tron",
            {"fields": ("tron_api_key",)},
        ),
    )

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return (
                *self.base_fieldsets,
                *self.evm_fieldsets,
                *self.tron_fieldsets,
            )
        if obj.type == ChainType.EVM:
            return (*self.base_fieldsets, *self.evm_fieldsets)
        if obj.type == ChainType.TRON:
            return (*self.base_fieldsets, *self.tron_fieldsets)
        return self.base_fieldsets


@admin.register(Wallet)
class WalletAdmin(ReadOnlyModelAdmin):
    list_display = ("__str__",)


@admin.register(Address)
class AddressAdmin(ReadOnlyModelAdmin):
    list_display = (
        "address",
        "usage",
    )
    readonly_fields = (
        "address",
        "wallet",
        "usage",
        "chain_type",
        "bip44_account",
        "address_index",
    )


@admin.register(Transfer)
class TransferAdmin(ReadOnlyModelAdmin):
    search_fields = ("hash",)
    readonly_fields = ("display_crypto", "display_chain")

    list_display = (
        "from_address",
        "to_address",
        "chain",
        "crypto",
        "amount",
        "datetime",
        "type",
        "display_status",
    )

    fields = (
        "from_address",
        "to_address",
        "display_chain",
        "display_crypto",
        "value",
        "amount",
        "block",
        "hash",
        "datetime",
        "timestamp",
        "type",
    )

    @display(description=_("加密货币"))  # noqa
    def display_crypto(self, obj: Transfer):
        return obj.crypto.symbol

    @display(description=_("链"))  # noqa
    def display_chain(self, obj: Transfer):
        return obj.chain.name

    @display(
        description="状态",
        label={
            "确认中": "info",
            "已确认": "success",
            "已失效": "",
        },
    )
    def display_status(self, instance: Transfer):
        return instance.get_status_display()


@admin.register(TxTask)
class TxTaskAdmin(ReadOnlyModelAdmin):
    # TxTask 是跨链统一锚点；后台只做观察与排障，禁止人工修改，避免破坏 stage/success 二元一致约束。
    ordering = ("-created_at",)
    list_display = (
        "display_address",
        "display_chain",
        "display_tx_type",
        "display_tx_hash",
        "display_status",
        "created_at",
    )
    list_filter = ("stage", "success", "tx_type", "chain")
    list_select_related = ("address", "chain")
    search_fields = ("tx_hash", "address__address")

    @admin.display(ordering="address__address", description=_("地址"))
    def display_address(self, obj: TxTask):
        return obj.address

    @admin.display(ordering="chain__name", description=_("网络"))
    def display_chain(self, obj: TxTask):
        return obj.chain

    @admin.display(ordering="tx_type", description=_("类型"))
    def display_tx_type(self, obj: TxTask):
        return obj.get_tx_type_display()

    @admin.display(ordering="tx_hash", description=_("交易哈希"))
    def display_tx_hash(self, obj: TxTask):
        return obj.tx_hash or "—"

    @display(
        description=_("状态"),
        label={
            "待广播": "warning",
            "待上链": "warning",
            "确认中": "info",
            "成功": "success",
            "失败": "danger",
            "已完结": "info",
        },
    )
    def display_status(self, instance: TxTask):
        # TxTask.display_status 已将 stage/success 融合为面向运营的单字段语义，
        # 这里沿用同一来源避免后台与业务代码的展示口径漂移。
        return instance.display_status
