from decimal import Decimal

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django_celery_results.models import TaskResult

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
            {
                "fields": (
                    "evm_vault_slot_collect_delay_minutes",
                    "tron_vault_slot_collect_delay_minutes",
                )
            },
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
    # 系统热钱包页是只读的基础设施概览，用专属模板渲染地址与各链余额仪表盘。
    change_form_template = "admin/core/systemwallet/change_form.html"
    # 页面不暴露任何可编辑字段，给一组空 fieldset 让 admin 表单机制安全空转。
    fieldsets = ((None, {"fields": ()}),)
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

    def change_view(self, request, object_id, form_url="", extra_context=None):
        if not self.has_view_permission(request):
            raise PermissionDenied
        instance = self.get_object(request, object_id)
        extra_context = {
            **(extra_context or {}),
            # 把地址派生与各链余额查询的结果整理成结构化数据交给模板渲染，
            # 视图层只负责取数与异常归一，展示完全交由模板控制。
            "wallet_overview": self.build_wallet_overview(instance),
        }
        return super().change_view(request, object_id, form_url, extra_context)

    def build_wallet_overview(self, instance: SystemWallet | None) -> dict | None:
        if instance is None:
            return None

        # 钱包地址按链类型分卡展示：EVM 全链共用同一地址，Tron 独立一份。
        address_cards = []
        for chain_type, icon in (
            (ChainType.EVM, "hub"),
            (ChainType.TRON, "bolt"),
        ):
            address, error = self.resolve_chain_type_address(instance, chain_type)
            address_cards.append(
                {
                    "label": chain_type.label,
                    "icon": icon,
                    "address": address,
                    "error": error,
                }
            )

        # 余额只覆盖启用的 EVM 链：Tron 原生币余额查询当前有意推迟，不在此展示。
        evm_address, evm_error = self.resolve_chain_type_address(
            instance, ChainType.EVM
        )
        balance_rows = [
            self.build_balance_row(chain, evm_address, evm_error)
            for chain in Chain.objects.filter(
                type=ChainType.EVM, active=True
            ).order_by("code")
        ]
        return {
            "address_cards": address_cards,
            "balance_rows": balance_rows,
            # EVM 地址整体派生失败时，余额区直接展示同一错误横幅，不再逐链重复。
            "balance_error": evm_error,
            "has_evm_chains": bool(balance_rows),
        }

    def build_balance_row(
        self, chain: Chain, address: str | None, address_error: str | None
    ) -> dict:
        """把单条 EVM 链的原生币余额查询结果归一成模板可直接渲染的结构。

        status 取值：ok（查到余额）、no_rpc（未配置 RPC）、unsupported（适配器不支持）、
        error（地址派生或链上查询异常）。note 承载非 ok 状态下的提示文案。
        """
        row = {
            "name": chain.name,
            "symbol": chain.spec.native_coin_symbol,
            "is_testnet": chain.is_testnet,
            "balance": None,
            "status": "ok",
            "note": None,
        }
        if address_error:
            return {**row, "status": "error", "note": address_error}
        if not chain.rpc:
            return {**row, "status": "no_rpc", "note": _("RPC 未配置")}
        try:
            raw_balance = chain.adapter.get_balance(address, chain, chain.native_coin)
        except NotImplementedError:
            return {**row, "status": "unsupported", "note": _("暂不支持查询")}
        except Exception as exc:  # noqa: BLE001
            return {**row, "status": "error", "note": _("查询失败：%(err)s") % {"err": exc}}

        decimals = self.resolve_native_decimals(chain)
        amount = Decimal(raw_balance).scaleb(-decimals)
        row["balance"] = format_decimal_stripped(amount)
        return row

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

    def resolve_native_decimals(self, chain: Chain) -> int:
        try:
            return chain.native_coin.get_decimals(chain)
        except Exception:  # noqa: BLE001
            return chain.spec.native_coin_decimals
