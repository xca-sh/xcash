from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from unfold.decorators import display

from common.admin import ModelAdmin
from common.admin import TabularInline
from currencies.models import Crypto
from currencies.models import CryptoOnChain
from currencies.models import Fiat


class CryptoOnChainInline(TabularInline):
    model = CryptoOnChain
    extra = 0
    verbose_name = _("链上币种")
    verbose_name_plural = _("链上币种")
    fields = ("chain", "address", "decimals", "active")


@admin.register(Crypto)
class CryptoAdmin(ModelAdmin):
    inlines = (CryptoOnChainInline,)

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("crypto_on_chains__chain")

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return "symbol", "prices"
        return ()

    list_display = (
        "name",
        "symbol",
        "supported_chains",
        "display_type",
        "active",
    )
    list_filter = ("active", "is_native")
    readonly_fields = ("is_native",)

    @display(
        description=_("类型"),
        label={
            "native": "warning",
            "token": "info",
        },
    )
    def display_type(self, instance: Crypto):
        if instance.is_native:
            return ("native", _("原生币"))
        return ("token", _("代币"))


@admin.register(Fiat)
class FiatAdmin(ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        if obj:
            return ("code",)
        return ()

    list_display = (
        "code",
        "icon",
    )
