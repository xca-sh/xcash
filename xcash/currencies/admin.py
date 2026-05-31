from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from unfold.admin import TabularInline
from unfold.decorators import display

from common.admin import ModelAdmin
from currencies.models import ChainToken
from currencies.models import Crypto
from currencies.models import Fiat


class ChainTokenInline(TabularInline):
    model = ChainToken
    extra = 0
    verbose_name = _("链上部署")
    verbose_name_plural = _("链上部署")
    fields = ("chain", "address", "decimals", "active")


@admin.register(Crypto)
class CryptoAdmin(ModelAdmin):
    inlines = (ChainTokenInline,)

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return "symbol", "prices"
        return ()

    list_display = (
        "name",
        "symbol",
        "supported_chains",
        "display_type",
        "is_native",
        "active",
    )
    list_filter = ("active", "is_native")

    @display(
        description="类型",
        label={
            "原生币": "warning",
            "代币": "info",
        },
    )
    def display_type(self, instance: Crypto):
        if instance.is_native:
            return "原生币"
        return "代币"


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
