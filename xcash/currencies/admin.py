from django.contrib import admin
from django.utils.html import format_html
from django.utils.html import format_html_join
from django.utils.translation import gettext_lazy as _
from unfold.decorators import display

from common import admin_display as fmt
from common.admin import ModelAdmin
from common.admin import TabularInline
from currencies.models import Crypto
from currencies.models import CryptoOnChain
from currencies.models import Fiat


class CryptoOnChainInline(TabularInline):
    model = CryptoOnChain
    extra = 0
    show_count = True
    show_title = False
    verbose_name = _("链上形态")
    verbose_name_plural = _("链上形态")
    fields = ("chain", "address", "decimals", "active")
    ordering = ("-pk",)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("chain")


@admin.register(Crypto)
class CryptoAdmin(ModelAdmin):
    inlines = (CryptoOnChainInline,)
    ordering = ("-pk",)
    list_display = (
        "display_identity",
        "display_type",
        "display_supported_chains",
        "display_usd_price",
        "display_payable",
        "prices_updated_at",
        "active",
    )
    list_editable = ("active",)
    list_filter = ("active", "is_native")
    search_fields = ("symbol", "name", "coingecko_id")
    search_help_text = _("支持按代码、名称或 CoinGecko ID 搜索")
    readonly_fields = ("is_native", "display_prices")
    fieldsets = (
        (
            _("基本信息"),
            {
                "classes": ("tab",),
                "fields": (
                    "name",
                    "symbol",
                    "is_native",
                    "active",
                ),
            },
        ),
        (
            _("行情"),
            {
                "classes": ("tab",),
                "fields": (
                    "coingecko_id",
                    "prices_updated_at",
                    "display_prices",
                ),
                "description": _(
                    "未配置 CoinGecko ID 且非 USD 锚定稳定币的资产不具备法币计价能力，"
                    "只能用于充值收款，不会出现在账单收款可选方式中。"
                ),
            },
        ),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("crypto_on_chains__chain")

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return ("symbol", "is_native", "display_prices")
        return ("is_native", "display_prices")

    @display(description=_("加密货币"), ordering="symbol", header=True)
    def display_identity(self, instance: Crypto):
        return (instance.symbol, instance.name)

    @display(
        description=_("类型"),
        ordering="is_native",
        label={
            "native": "warning",
            "token": "info",
        },
    )
    def display_type(self, instance: Crypto):
        if instance.is_native:
            return ("native", _("原生币"))
        return ("token", _("代币"))

    @display(description=_("支持的链"))
    def display_supported_chains(self, instance: Crypto):
        # 链名列表直接渲染成灰底小标签，比逗号分隔的长串更容易扫。
        on_chains = list(instance.crypto_on_chains.all())
        if not on_chains:
            return fmt.empty()
        return format_html(
            '<span class="flex flex-wrap gap-1">{}</span>',
            format_html_join(
                "",
                '<span class="inline-block rounded-default bg-base-500/8 px-2 '
                "text-[11px] leading-5 text-base-700 dark:bg-base-500/20 "
                'dark:text-base-200">{}</span>',
                ((on_chain.chain.code,) for on_chain in on_chains),
            ),
        )

    @display(description=_("USD 价格"))
    def display_usd_price(self, instance: Crypto):
        price = (instance.prices or {}).get("USD")
        if price is None:
            return fmt.empty()
        return fmt.number(price, unit="USD")

    @display(
        description=_("可计价"),
        label={"yes": "success", "no": ""},
    )
    def display_payable(self, instance: Crypto):
        return ("yes", _("是")) if instance.is_payable else ("no", _("否"))

    @display(description=_("行情快照"))
    def display_prices(self, instance: Crypto):
        return fmt.scroll_box(instance.prices)


@admin.register(Fiat)
class FiatAdmin(ModelAdmin):
    ordering = ("-pk",)
    list_display = ("display_identity",)
    search_fields = ("code",)
    search_help_text = _("支持按法币代码搜索")
    fields = ("code",)

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return ("code",)
        return ()

    @display(description=_("法定货币"), ordering="code", header=True)
    def display_identity(self, instance: Fiat):
        return (instance.code, instance.icon)
