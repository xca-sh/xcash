from django.contrib import admin
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _
from tron.models import TronTxTask
from unfold.contrib.filters.admin import ChoicesDropdownFilter
from unfold.contrib.filters.admin import RangeDateTimeFilter
from unfold.contrib.filters.admin import RelatedDropdownFilter
from unfold.decorators import display

from chains.admin import TX_TASK_STATUS_LABELS
from common import admin_display as fmt
from common.admin import ReadOnlyModelAdmin


@admin.register(TronTxTask)
class TronTxTaskAdmin(ReadOnlyModelAdmin):
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    exclude = ("signed_payload",)
    readonly_fields = (
        "base_task",
        "display_full_sender",
        "chain",
        "display_tx_type_text",
        "display_status_text",
        "display_full_to",
        "function_selector",
        "display_parameter",
        "fee_limit",
        "display_full_tx_id",
        "display_expiration_text",
        "expiration",
        "ref_block_bytes",
        "ref_block_hash",
        "simulation_revert_count",
        "simulation_revert_first_at",
        "formatted_last_attempt_at",
        "created_at",
    )
    list_display = (
        "display_tx_id",
        "display_chain",
        "display_tx_type",
        "display_sender",
        "display_to",
        "display_status",
        "display_expiration_state",
        "created_at",
        "formatted_last_attempt_at",
    )
    list_filter = (
        ("base_task__status", ChoicesDropdownFilter),
        ("base_task__tx_type", ChoicesDropdownFilter),
        ("chain", RelatedDropdownFilter),
        ("created_at", RangeDateTimeFilter),
    )
    list_select_related = ("base_task", "sender", "chain")
    search_fields = ("base_task__tx_hash", "tx_id", "sender__address", "to")
    search_help_text = _("支持按交易哈希、tx_id、发送地址或目标地址搜索")
    fieldsets = (
        (
            _("任务"),
            {
                "classes": ("tab",),
                "fields": (
                    "base_task",
                    "display_tx_type_text",
                    "display_status_text",
                    "chain",
                    "created_at",
                    "formatted_last_attempt_at",
                ),
            },
        ),
        (
            _("合约调用"),
            {
                "classes": ("tab",),
                "fields": (
                    "display_full_sender",
                    "display_full_to",
                    "function_selector",
                    "display_parameter",
                    "fee_limit",
                ),
                "description": _(
                    "签名产物 signed_payload 不在后台展示：它可被任何人直接广播，等同一次性支付凭证。"
                ),
            },
        ),
        (
            _("广播与过期"),
            {
                "classes": ("tab",),
                "fields": (
                    "display_full_tx_id",
                    "display_expiration_text",
                    "expiration",
                    "ref_block_bytes",
                    "ref_block_hash",
                    "simulation_revert_count",
                    "simulation_revert_first_at",
                ),
            },
        ),
    )

    @admin.display(description=_("执行时间"), ordering="last_attempt_at")
    def formatted_last_attempt_at(self, obj: TronTxTask):
        if obj.last_attempt_at:
            return date_format(
                timezone.localtime(obj.last_attempt_at), "DATETIME_FORMAT"
            )
        return None

    @display(
        description=_("状态"),
        ordering="base_task__status",
        label=TX_TASK_STATUS_LABELS,
    )
    def display_status(self, obj: TronTxTask):
        return (obj.base_task.status, obj.status)

    @display(
        description=_("过期"),
        label={
            "unsigned": "",
            "valid": "success",
            "expired": "warning",
        },
    )
    def display_expiration_state(self, obj: TronTxTask) -> str:
        if obj.expiration is None:
            return ("unsigned", _("未签名"))
        return ("expired", _("已过期")) if obj.is_expired() else ("valid", _("有效"))

    @display(description=_("类型"), ordering="base_task__tx_type", label=True)
    def display_tx_type(self, obj: TronTxTask):  # pragma: no cover
        return obj.base_task.get_tx_type_display() if obj.base_task_id else fmt.empty()

    @display(description=_("类型"))
    def display_tx_type_text(self, obj: TronTxTask):  # pragma: no cover
        # 详情页不能复用带 label 的 display：unfold 的标签渲染只作用于列表页，
        # 放进 fieldsets 会把 (value, text) 元组原样打印出来。
        return obj.base_task.get_tx_type_display() if obj.base_task_id else fmt.empty()

    @display(description=_("状态"))
    def display_status_text(self, obj: TronTxTask):  # pragma: no cover
        return obj.status

    @display(description=_("过期"))
    def display_expiration_text(self, obj: TronTxTask) -> str:
        if obj.expiration is None:
            return _("未签名")
        return _("已过期") if obj.is_expired() else _("有效")

    @display(description=_("发送地址"), ordering="sender__address")
    def display_sender(self, obj: TronTxTask):  # pragma: no cover
        return fmt.truncated(obj.sender.address)

    @display(description=_("发送地址"))
    def display_full_sender(self, obj: TronTxTask):  # pragma: no cover
        return fmt.mono(obj.sender.address)

    @display(description=_("目标地址"), ordering="to")
    def display_to(self, obj: TronTxTask):  # pragma: no cover
        return fmt.truncated(obj.to)

    @display(description=_("目标地址"))
    def display_full_to(self, obj: TronTxTask):  # pragma: no cover
        return fmt.mono(obj.to)

    @display(description="tx_id", ordering="tx_id")
    def display_tx_id(self, obj: TronTxTask):  # pragma: no cover
        return fmt.truncated(obj.tx_id)

    @display(description="tx_id")
    def display_full_tx_id(self, obj: TronTxTask):  # pragma: no cover
        return fmt.mono(obj.tx_id)

    @display(description=_("调用参数"))
    def display_parameter(self, obj: TronTxTask):  # pragma: no cover
        return fmt.scroll_box(obj.parameter)

    @display(description=_("网络"), ordering="chain__code")
    def display_chain(self, obj: TronTxTask):  # pragma: no cover
        return obj.chain
