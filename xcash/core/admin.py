from decimal import Decimal

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django_celery_results.models import GroupResult
from django_celery_results.models import TaskResult
from unfold.contrib.filters.admin import ChoicesDropdownFilter
from unfold.contrib.filters.admin import RangeDateTimeFilter
from unfold.decorators import display

from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from common import admin_display as fmt
from common.admin import ModelAdmin
from common.admin import ReadOnlyModelAdmin
from common.utils.math import format_decimal_stripped
from core.models import SystemSettings
from core.models import SystemWallet

# django_celery_results 自带的 admin 是原生 django.contrib.admin.ModelAdmin，
# 在 unfold 下拿不到筛选抽屉、标签等能力，统一换成项目基类重新注册。
admin.site.unregister(TaskResult)
admin.site.unregister(GroupResult)

# Celery 任务状态取值来自 celery.states，非 Django choices，这里显式给配色。
TASK_RESULT_STATUS_LABELS = {
    "SUCCESS": "success",
    "FAILURE": "danger",
    "RETRY": "warning",
    "REVOKED": "danger",
    "STARTED": "info",
    "PENDING": "",
    "RECEIVED": "",
}


@admin.register(TaskResult)
class TaskResultAdmin(ReadOnlyModelAdmin):
    date_hierarchy = "date_done"
    ordering = ("-date_done",)
    list_display = (
        "display_identity",
        "display_status",
        "worker",
        "date_created",
        "date_done",
    )
    list_filter = (
        ("status", ChoicesDropdownFilter),
        "task_name",
        "worker",
        ("date_done", RangeDateTimeFilter),
    )
    search_fields = ("task_id", "task_name")
    search_help_text = _("支持按任务 ID 或任务名搜索")
    readonly_fields = (
        "task_id",
        "task_name",
        "status",
        "worker",
        "date_created",
        "date_done",
        "display_args",
        "display_kwargs",
        "display_result",
        "display_traceback",
    )
    fieldsets = (
        (
            _("任务"),
            {
                "classes": ("tab",),
                "fields": (
                    "task_id",
                    "task_name",
                    "status",
                    "worker",
                    "date_created",
                    "date_done",
                ),
            },
        ),
        (
            _("入参"),
            {
                "classes": ("tab",),
                "fields": ("display_args", "display_kwargs"),
            },
        ),
        (
            _("结果"),
            {
                "classes": ("tab",),
                "fields": ("display_result", "display_traceback"),
            },
        ),
    )

    @display(description=_("任务"), ordering="task_name", header=True)
    def display_identity(self, obj: TaskResult):
        return (obj.task_name or fmt.EMPTY_VALUE, obj.task_id)

    @display(
        description=_("状态"),
        ordering="status",
        label=TASK_RESULT_STATUS_LABELS,
    )
    def display_status(self, obj: TaskResult):
        return (obj.status, obj.status)

    @display(description=_("位置参数"))
    def display_args(self, obj: TaskResult):
        return fmt.scroll_box(obj.task_args)

    @display(description=_("关键字参数"))
    def display_kwargs(self, obj: TaskResult):
        return fmt.scroll_box(obj.task_kwargs)

    @display(description=_("返回值"))
    def display_result(self, obj: TaskResult):
        return fmt.scroll_box(obj.result)

    @display(description=_("异常堆栈"))
    def display_traceback(self, obj: TaskResult):
        return fmt.scroll_box(obj.traceback)


@admin.register(GroupResult)
class GroupResultAdmin(ReadOnlyModelAdmin):
    date_hierarchy = "date_done"
    ordering = ("-date_done",)
    list_display = ("group_id", "date_created", "date_done")
    search_fields = ("group_id",)
    search_help_text = _("支持按任务组 ID 搜索")
    readonly_fields = ("group_id", "date_created", "date_done", "display_result")
    fields = readonly_fields

    @display(description=_("结果"))
    def display_result(self, obj: GroupResult):
        return fmt.scroll_box(obj.result)


@admin.register(SystemSettings)
class SystemSettingsAdmin(ModelAdmin):
    fieldsets = (
        (
            _("后台安全"),
            {
                "classes": ("tab",),
                "fields": ("admin_session_timeout_minutes",),
            },
        ),
        (
            _("Webhook"),
            {
                "classes": ("tab",),
                "fields": (
                    "webhook_delivery_max_retries",
                    "webhook_delivery_max_backoff_seconds",
                    "webhook_event_timeout_minutes",
                ),
                "description": _(
                    "重试次数与退避上限决定单个事件的最长投递周期；超时分钟数只影响异常巡检的判定口径。"
                ),
            },
        ),
        (
            "VaultSlot",
            {
                "classes": ("tab",),
                "fields": (
                    "evm_vault_slot_collect_delay_minutes",
                    "tron_vault_slot_collect_delay_minutes",
                    "vault_slot_collect_min_worth_usd",
                    "invoice_vault_slot_limit_per_project_chain",
                ),
                "description": _(
                    "归集延迟用于把同一槽位的多次到账聚合成一笔交易；最小归集价值避免为尘埃余额支付 gas。"
                ),
            },
        ),
        (
            _("AML 筛查"),
            {
                "classes": ("tab",),
                "fields": (
                    "misttrack_openapi_api_key",
                    "quicknode_misttrack_endpoint_url",
                ),
                "description": _(
                    "AML 开关与查询阈值已改为按项目配置，请在项目详情页设置。"
                ),
            },
        ),
        (
            _("审计"),
            {
                "classes": ("tab",),
                "fields": (
                    "created_by",
                    "updated_by",
                    "created_at",
                    "updated_at",
                ),
            },
        ),
    )
    readonly_fields = ("created_by", "updated_by", "created_at", "updated_at")
    list_display = (
        "id",
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
            for chain in Chain.objects.filter(type=ChainType.EVM, active=True).order_by(
                "code"
            )
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
            return {
                **row,
                "status": "error",
                "note": _("查询失败：%(err)s") % {"err": exc},
            }

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
