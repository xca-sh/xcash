from aml.models import RiskAssessment
from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from unfold.contrib.filters.admin import ChoicesDropdownFilter
from unfold.contrib.filters.admin import RangeDateTimeFilter
from unfold.decorators import display

from chains.models import TransferType
from common import admin_display as fmt
from common.admin import ReadOnlyModelAdmin


@admin.register(RiskAssessment)
class RiskAssessmentAdmin(ReadOnlyModelAdmin):
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("transfer", "transfer__chain")
    list_filter_sheet = False
    list_display = (
        "display_target",
        "display_transfer_type",
        "display_risk",
        "display_status",
        "source",
        "display_address",
        "checked_at",
        "created_at",
    )
    list_filter = (
        ("risk_level", ChoicesDropdownFilter),
        ("status", ChoicesDropdownFilter),
        "source",
        ("created_at", RangeDateTimeFilter),
    )
    search_fields = (
        "address",
        "tx_hash",
    )
    search_help_text = _("支持按付款地址或交易哈希搜索")
    readonly_fields = (
        "source",
        "status",
        "transfer",
        "display_full_address",
        "display_full_tx_hash",
        "risk_level",
        "risk_score",
        "display_raw_response",
        "display_error_message",
        "checked_at",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (
            _("筛查对象"),
            {
                "classes": ("tab",),
                "fields": (
                    "transfer",
                    "display_full_address",
                    "display_full_tx_hash",
                ),
            },
        ),
        (
            _("风险结果"),
            {
                "classes": ("tab",),
                "fields": (
                    "source",
                    "status",
                    "risk_level",
                    "risk_score",
                    "display_error_message",
                    "checked_at",
                    "created_at",
                    "updated_at",
                ),
            },
        ),
        (
            _("原始响应"),
            {
                "classes": ("tab",),
                "fields": ("display_raw_response",),
            },
        ),
    )

    @display(description=_("筛查对象"), header=True)
    def display_target(self, instance: RiskAssessment):
        # 评估挂在转账上：列表页展示付款地址与所在链；历史记录可能已无关联转账。
        chain = instance.transfer.chain.code if instance.transfer_id else ""
        return (fmt.truncated(instance.address), chain)

    @display(
        description=_("类型"),
        label={
            TransferType.Invoice: "primary",
            TransferType.Deposit: "info",
        },
    )
    def display_transfer_type(self, instance: RiskAssessment):
        if not instance.transfer_id:
            return None
        return (instance.transfer.type, instance.transfer.get_type_display())

    @display(
        description=_("风险"),
        ordering="risk_level",
        label={
            "Low": "success",
            "Moderate": "warning",
            "High": "danger",
            "Severe": "danger",
        },
    )
    def display_risk(self, instance: RiskAssessment):
        if not instance.risk_level:
            return None
        return (instance.risk_level, f"{instance.risk_level} · {instance.risk_score}")

    @display(
        description=_("查询状态"),
        ordering="status",
        label={
            RiskAssessment.Status.SUCCESS: "success",
            RiskAssessment.Status.FAILED: "danger",
        },
    )
    def display_status(self, instance: RiskAssessment):
        return (instance.status, instance.get_status_display())

    @display(description=_("地址 / 哈希"))
    def display_address(self, instance: RiskAssessment):
        return fmt.truncated(instance.address or instance.tx_hash)

    @display(description=_("地址"))
    def display_full_address(self, instance: RiskAssessment):
        return fmt.mono(instance.address)

    @display(description=_("交易哈希"))
    def display_full_tx_hash(self, instance: RiskAssessment):
        return fmt.mono(instance.tx_hash)

    @display(description=_("原始响应"))
    def display_raw_response(self, instance: RiskAssessment):
        return fmt.scroll_box(instance.raw_response)

    @display(description=_("错误信息"))
    def display_error_message(self, instance: RiskAssessment):
        return fmt.scroll_box(instance.error_message)
