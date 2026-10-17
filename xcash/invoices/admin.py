from __future__ import annotations

from aml.models import transfer_assessment
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.contrib.filters.admin import ChoicesDropdownFilter
from unfold.contrib.filters.admin import RangeDateTimeFilter
from unfold.contrib.filters.admin import RelatedDropdownFilter
from unfold.decorators import display

from common import admin_display as fmt
from common.admin import ModelAdmin
from common.admin import ReadOnlyModelAdmin
from common.admin import StackedInline

from .models import DifferRecipientAddress
from .models import EpayOrder
from .models import Invoice
from .models import InvoiceProtocol
from .models import InvoiceStatus


class EpayOrderInline(StackedInline):
    # EpayOrder 与 Invoice 是 OneToOne，限制 max_num=1 让表单语义对齐数据约束。
    model = EpayOrder
    extra = 0
    max_num = 1
    can_delete = False
    tab = True
    verbose_name = _("EPay 订单")
    verbose_name_plural = _("EPay 订单")
    fields = (
        "trade_no",
        "out_trade_no",
        "merchant",
        "pid",
        "type",
        "money",
        "sign_type",
        "notify_url",
        "return_url",
        "param",
        "notify_event",
        "created_at",
    )
    readonly_fields = (
        "trade_no",
        "out_trade_no",
        "merchant",
        "pid",
        "type",
        "money",
        "sign_type",
        "notify_url",
        "return_url",
        "param",
        "notify_event",
        "created_at",
    )

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(DifferRecipientAddress)
class DifferRecipientAddressAdmin(ModelAdmin):
    list_display = (
        "display_address",
        "project",
        "display_chain_type",
        "active",
        "sort_order",
        "created_at",
    )
    list_editable = (
        "active",
        "sort_order",
    )
    list_filter = (
        ("chain_type", ChoicesDropdownFilter),
        "active",
        ("project", RelatedDropdownFilter),
        "project__is_test",
    )
    list_select_related = ("project",)
    search_fields = (
        "project__name",
        "project__appid",
        "address",
    )
    search_help_text = _("支持按项目名称、Appid 或收款地址搜索")
    readonly_fields = ("created_at",)
    fieldsets = (
        (
            _("收款地址"),
            {
                "fields": (
                    "project",
                    "chain_type",
                    "address",
                ),
                "description": _(
                    "钱包直收模式下，账单收款直接打到这里配置的外部地址；同一链类型可配置多条并按排序轮换。"
                ),
            },
        ),
        (
            _("启用与排序"),
            {"fields": ("active", "sort_order", "created_at")},
        ),
    )

    def get_readonly_fields(self, request, obj=None):
        # 创建后地址、链类型与归属项目不可修改，见 DifferRecipientAddress.IMMUTABLE_FIELDS。
        if obj is not None:
            return ("project", "chain_type", "address", "created_at")
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.has_unfinished_invoices():
            return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        # 批量删除走 QuerySet.delete()，会绕过逐条的未完结账单检查，因此关闭。
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    @display(description=_("收款地址"), ordering="address")
    def display_address(self, obj: DifferRecipientAddress):
        return fmt.truncated(obj.address)

    @display(
        description=_("链类型"),
        ordering="chain_type",
        label={"evm": "info", "tron": "warning"},
    )
    def display_chain_type(self, obj: DifferRecipientAddress):
        return (obj.chain_type, obj.get_chain_type_display())


@admin.register(Invoice)
class InvoiceAdmin(ReadOnlyModelAdmin):
    inlines = (EpayOrderInline,)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = (
        "project",
        "crypto",
        "chain",
        "currency",
        "transfer__aml_assessment",
    )
    list_filter_sheet = False

    # 列表页按「是谁的单 → 收多少 → 收什么 → 到哪一步 → 有无风险」的阅读顺序编排，
    # 强相关字段合并成一列，避免横向滚动才能看全一笔账单。
    list_display = (
        "display_identity",
        "project",
        "display_amount",
        "display_pay_amount",
        "display_network",
        "display_status",
        "display_protocol",
        "display_risk",
        "created_at",
        "display_pay_url",
    )
    search_fields = (
        "sys_no",
        "out_no",
        "transfer__hash",
        "pay_address",
    )
    search_help_text = _("支持按系统单号、商户单号、链上交易哈希或收款地址搜索")
    list_filter = (
        ("status", ChoicesDropdownFilter),
        ("protocol", ChoicesDropdownFilter),
        ("chain", RelatedDropdownFilter),
        ("crypto", RelatedDropdownFilter),
        ("project", RelatedDropdownFilter),
        ("transfer__aml_assessment__risk_level", ChoicesDropdownFilter),
        ("created_at", RangeDateTimeFilter),
    )
    readonly_fields = (
        "display_detail_crypto",
        "display_detail_chain",
        "display_detail_risk",
        "display_detail_risk_score",
        "display_detail_pay_url",
        "display_detail_methods",
    )
    fieldsets = (
        (
            _("订单"),
            {
                "classes": ("tab",),
                "fields": (
                    "sys_no",
                    "out_no",
                    "project",
                    "protocol",
                    "title",
                    "status",
                    "created_at",
                    "expires_at",
                ),
            },
        ),
        (
            _("金额"),
            {
                "classes": ("tab",),
                "fields": (
                    "currency",
                    "amount",
                    "worth",
                    "display_detail_methods",
                ),
            },
        ),
        (
            _("链上收款"),
            {
                "classes": ("tab",),
                "fields": (
                    "display_detail_crypto",
                    "display_detail_chain",
                    "pay_amount",
                    "pay_address",
                    "transfer",
                    "display_detail_pay_url",
                ),
            },
        ),
        (
            _("风控"),
            {
                "classes": ("tab",),
                "fields": ("display_detail_risk", "display_detail_risk_score"),
            },
        ),
        (
            _("回调"),
            {
                "classes": ("tab",),
                "fields": ("notify_url", "return_url"),
            },
        ),
    )

    def get_inline_instances(self, request, obj=None):
        # 非 EPay 协议账单没有 EpayOrder 数据，隐藏空 inline 避免界面噪音。
        inline_instances = super().get_inline_instances(request, obj)
        if obj is None or obj.protocol != InvoiceProtocol.EPAY_V1:
            inline_instances = [
                inline
                for inline in inline_instances
                if not isinstance(inline, EpayOrderInline)
            ]
        return inline_instances

    @display(description=_("单号"), ordering="sys_no", header=True)
    def display_identity(self, instance: Invoice):
        # header 展示为两行：系统单号在上、商户单号在下，省掉一整列还更好扫读。
        return (
            instance.sys_no,
            _("商户单号 %(out_no)s") % {"out_no": instance.out_no},
        )

    @display(
        description=_("状态"),
        ordering="status",
        label={
            InvoiceStatus.WAITING: "warning",
            InvoiceStatus.COMPLETED: "success",
            InvoiceStatus.EXPIRED: "",
        },
    )
    def display_status(self, instance: Invoice):
        return (instance.status, instance.get_status_display())

    @display(
        description=_("风险"),
        ordering="transfer__aml_assessment__risk_level",
        label={
            "Low": "success",
            "Moderate": "warning",
            "High": "danger",
            "Severe": "danger",
        },
    )
    def display_risk(self, instance: Invoice):
        assessment = transfer_assessment(instance.transfer)
        if assessment is None or not assessment.risk_level:
            return None
        return (
            assessment.risk_level,
            f"{assessment.risk_level} · {assessment.risk_score}",
        )

    @display(
        description=_("协议"),
        ordering="protocol",
        label={
            InvoiceProtocol.NATIVE: "info",
            InvoiceProtocol.EPAY_V1: "primary",
        },
    )
    def display_protocol(self, instance: Invoice):
        return (instance.protocol, instance.get_protocol_display())

    @display(description=_("计价金额"), ordering="amount")
    def display_amount(self, instance: Invoice):
        # currency 为 Fiat FK，取 currency_id 直接拿法币 code，避免 __str__ 带上 icon。
        return fmt.number(instance.amount, unit=instance.currency_id)

    @display(description=_("收款数量"), ordering="pay_amount")
    def display_pay_amount(self, instance: Invoice):
        if instance.pay_amount is None:
            return fmt.empty()
        return fmt.number(
            instance.pay_amount,
            unit=instance.crypto.symbol if instance.crypto else "",
        )

    @display(description=_("网络"), ordering="chain")
    def display_network(self, instance: Invoice):
        return instance.chain.code if instance.chain else fmt.empty()

    @display(description=_("收款页"))
    def display_pay_url(self, instance: Invoice):
        return format_html(
            '<a class="text-primary-600 dark:text-primary-400" href="{}" target="_blank" rel="noopener">'
            '<span class="material-symbols-outlined align-middle text-base">open_in_new</span></a>',
            reverse("payment-invoice", kwargs={"sys_no": instance.sys_no}),
        )

    @display(description=_("收款页链接"))
    def display_detail_pay_url(self, instance: Invoice):
        url = reverse("payment-invoice", kwargs={"sys_no": instance.sys_no})
        return format_html(
            '<a class="text-primary-600 dark:text-primary-400" href="{}" target="_blank" rel="noopener">{}</a>',
            url,
            url,
        )

    @display(description=_("加密货币"))
    def display_detail_crypto(self, obj: Invoice):
        return obj.crypto.symbol if obj.crypto else fmt.empty()

    @display(description=_("链"))
    def display_detail_chain(self, obj: Invoice):
        return obj.chain.name if obj.chain else fmt.empty()

    @display(description=_("风险等级"))
    def display_detail_risk(self, instance: Invoice):
        assessment = transfer_assessment(instance.transfer)
        return (assessment and assessment.risk_level) or fmt.empty()

    @display(description=_("风险分数"))
    def display_detail_risk_score(self, instance: Invoice):
        assessment = transfer_assessment(instance.transfer)
        if assessment is None or assessment.risk_score is None:
            return fmt.empty()
        return assessment.risk_score

    @display(description=_("可选收款方式"))
    def display_detail_methods(self, instance: Invoice):
        # methods 是 {symbol: [chain_code]} 的 JSON，原样展示可读性差，压成一行标签。
        if not instance.methods:
            return fmt.empty()
        return format_html(
            '<span class="xc-mono">{}</span>',
            "  ".join(
                f"{symbol}: {', '.join(chains)}"
                for symbol, chains in instance.methods.items()
            ),
        )
