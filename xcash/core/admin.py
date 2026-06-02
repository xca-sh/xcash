from decimal import Decimal

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.html import format_html
from django.utils.html import format_html_join
from django.utils.translation import gettext_lazy as _
from django_celery_results.models import TaskResult
from unfold.decorators import display

from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from common.admin import ModelAdmin
from common.utils.math import format_decimal_stripped
from core.models import SystemSettings
from core.models import SystemWallet

admin.site.unregister(TaskResult)


@admin.register(TaskResult)
class TaskResultAdmin(ModelAdmin):
    list_display = ("task_id", "task_name", "status", "date_done")
    list_filter = ("status", "task_name", "date_done")


@admin.register(SystemSettings)
class SystemSettingsAdmin(ModelAdmin):
    fieldsets = (
        (
            "后台安全",
            {"fields": ("admin_session_timeout_minutes",)},
        ),
        (
            "Webhook 投递",
            {
                "fields": (
                    "webhook_delivery_breaker_threshold",
                    "webhook_delivery_max_retries",
                    "webhook_delivery_max_backoff_seconds",
                )
            },
        ),
        (
            "异常巡检",
            {"fields": ("webhook_event_timeout_minutes",)},
        ),
        (
            "VaultSlot",
            {"fields": ("vault_slot_collect_delay_minutes",)},
        ),
        (
            "AML 筛查",
            {
                "fields": (
                    "aml_screening_enabled",
                    "aml_screening_threshold_usd",
                    "aml_screening_cache_seconds",
                    "aml_screening_force_refresh_threshold_usd",
                    "misttrack_openapi_api_key",
                    "quicknode_misttrack_endpoint_url",
                )
            },
        ),
        (
            "审计",
            {
                "fields": (
                    "created_by",
                    "updated_by",
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )
    readonly_fields = ("created_by", "updated_by", "created_at", "updated_at")
    list_display = (
        "id",
        "aml_screening_enabled",
        "updated_by",
        "updated_at",
    )

    def has_module_permission(self, request):
        # 系统运行参数属于系统级治理能力，只向超管暴露模块入口。
        return bool(request.user.is_active and request.user.is_superuser)

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return (
            self.has_module_permission(request) and not SystemSettings.objects.exists()
        )

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_permission(request):
            raise PermissionDenied
        # 系统参数中心天然是单例，列表页直接收口到唯一那一份配置。
        config = SystemSettings.objects.order_by("pk").first()
        if config is not None:
            return redirect(
                reverse("admin:core_systemsettings_change", args=[config.pk])
            )
        return redirect(reverse("admin:core_systemsettings_add"))

    def save_model(self, request, obj, form, change):
        if change:
            obj.updated_by = request.user
        else:
            obj.created_by = request.user
            obj.updated_by = request.user
        # 系统运行参数需要保留明确的操作者审计，避免关键阈值被静默修改。
        super().save_model(request, obj, form, change)


@admin.register(SystemWallet)
class SystemWalletAdmin(ModelAdmin):
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "display_system_wallet_note",
                    "display_wallet_address",
                    "display_chain_balances",
                )
            },
        ),
    )
    readonly_fields = (
        "display_system_wallet_note",
        "display_wallet_address",
        "display_chain_balances",
    )
    list_display = ("id", "wallet", "updated_at")

    def has_module_permission(self, request):
        # 系统热钱包是平台基础设施入口，只向超管暴露。
        return bool(request.user.is_active and request.user.is_superuser)

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_permission(request):
            raise PermissionDenied
        system_wallet = SystemWallet.get_current()
        return redirect(
            reverse("admin:core_systemwallet_change", args=[system_wallet.pk])
        )

    @display(description=_("说明"))
    def display_system_wallet_note(self, instance: SystemWallet):
        return format_html(
            '<div class="max-w-3xl text-sm leading-6 text-base-600 dark:text-base-400">'
            "<p>{}</p>"
            '<p class="mt-2 font-medium text-amber-700 dark:text-amber-300">{}</p>'
            "</div>",
            _(
                "系统热钱包用于平台基础设施交易，例如 VaultSlot 合约部署、"
                "VaultSlot 归集等需要由系统主动发起的链上操作。"
            ),
            _(
                "这里只需要保留覆盖近期操作的小额 Gas，"
                "不要存入过多原生币，也不要把它作为业务资金归集地址。"
            ),
        )

    @display(description=_("钱包地址"))
    def display_wallet_address(self, instance: SystemWallet):
        address, error = self.resolve_chain_type_address(instance, ChainType.EVM)
        if error:
            return error
        if address is None:
            return "-"
        return format_html(
            '<div class="font-mono text-base break-all">{}</div>',
            address,
        )

    @display(description=_("各链余额"))
    def display_chain_balances(self, instance: SystemWallet):
        rows = self.build_chain_balance_rows(instance)
        if not rows:
            return _("暂无启用的 EVM 链")

        body = format_html_join(
            "",
            (
                "<tr>"
                '<td class="px-3 py-2 font-medium whitespace-nowrap">{}</td>'
                '<td class="px-3 py-2 whitespace-nowrap">{}</td>'
                "</tr>"
            ),
            rows,
        )
        return format_html(
            '<div class="overflow-auto">'
            '<table class="w-auto min-w-[480px] divide-y divide-base-200 text-sm">'
            "<thead>"
            "<tr>"
            '<th class="px-3 py-2 text-left">{}</th>'
            '<th class="px-3 py-2 text-left">{}</th>'
            "</tr>"
            "</thead>"
            "<tbody>{}</tbody>"
            "</table>"
            "</div>",
            _("链"),
            _("余额"),
            body,
        )

    def build_chain_balance_rows(self, instance: SystemWallet):
        rows = []
        address, address_error = self.resolve_chain_type_address(
            instance, ChainType.EVM
        )
        for chain in Chain.objects.filter(type=ChainType.EVM, active=True).order_by(
            "code"
        ):
            balance = (
                address_error
                if address_error
                else self.resolve_native_balance(chain=chain, address=address)
            )
            rows.append(
                (
                    chain.name,
                    balance,
                )
            )
        return rows

    def resolve_chain_type_address(
        self, instance: SystemWallet, chain_type: ChainType
    ) -> tuple[str | None, str | None]:
        try:
            address = instance.wallet.get_address(
                chain_type=chain_type,
                usage=AddressUsage.HotWallet,
            )
        except RuntimeError as exc:
            return None, _("地址派生失败：%(err)s") % {"err": exc}
        return address.address, None

    def resolve_native_balance(self, *, chain: Chain, address: str | None) -> str:
        if not address:
            return "-"
        if not chain.rpc:
            return _("未查询（RPC 未配置）")
        try:
            raw_balance = chain.adapter.get_balance(address, chain, chain.native_coin)
        except NotImplementedError:
            return _("暂不支持查询")
        except Exception as exc:  # noqa: BLE001
            return _("查询失败：%(err)s") % {"err": exc}

        decimals = self.resolve_native_decimals(chain)
        amount = Decimal(raw_balance).scaleb(-decimals)
        return f"{format_decimal_stripped(amount)} {chain.spec.native_coin_symbol}"

    def resolve_native_decimals(self, chain: Chain) -> int:
        try:
            return chain.native_coin.get_decimals(chain)
        except Exception:  # noqa: BLE001
            return chain.spec.native_coin_decimals
