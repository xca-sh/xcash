import structlog
from aml.models import transfer_assessment
from django.contrib import admin
from django.contrib import messages
from django.utils.translation import gettext_lazy as _
from unfold.contrib.filters.admin import ChoicesDropdownFilter
from unfold.contrib.filters.admin import RangeDateTimeFilter
from unfold.contrib.filters.admin import RelatedDropdownFilter
from unfold.decorators import display

from common import admin_display as fmt
from common.admin import ReadOnlyModelAdmin
from deposits.exceptions import DepositStatusError
from deposits.models import Deposit
from deposits.service import DepositService

logger = structlog.get_logger()


@admin.register(Deposit)
class DepositAdmin(ReadOnlyModelAdmin):
    actions = ("reschedule_erc20_collect",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_filter_sheet = False
    list_display = (
        "display_identity",
        "display_project",
        "display_amount",
        "display_worth",
        "display_network",
        "display_status",
        "display_risk",
        "display_created_at",
    )
    search_fields = ("sys_no", "customer__uid", "transfer__hash")
    search_help_text = _("支持按充值单号、客户 UID 或链上交易哈希搜索")
    list_filter = (
        ("transfer__status", ChoicesDropdownFilter),
        ("transfer__chain", RelatedDropdownFilter),
        ("transfer__crypto", RelatedDropdownFilter),
        ("customer__project", RelatedDropdownFilter),
        ("transfer__aml_assessment__risk_level", ChoicesDropdownFilter),
        ("created_at", RangeDateTimeFilter),
    )
    readonly_fields = (
        "sys_no",
        "customer",
        "display_project",
        "transfer",
        "display_amount",
        "display_network",
        "display_tx_hash",
        "worth",
        "display_status_text",
        "display_risk_level",
        "display_risk_score",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (
            _("充值"),
            {
                "classes": ("tab",),
                "fields": (
                    "sys_no",
                    "display_project",
                    "customer",
                    "display_status_text",
                    "created_at",
                    "updated_at",
                ),
            },
        ),
        (
            _("链上到账"),
            {
                "classes": ("tab",),
                "fields": (
                    "display_network",
                    "display_amount",
                    "worth",
                    "display_tx_hash",
                    "transfer",
                ),
            },
        ),
        (
            _("风控"),
            {
                "classes": ("tab",),
                "fields": ("display_risk_level", "display_risk_score"),
            },
        ),
    )

    @display(description=_("充值单号"), ordering="sys_no", header=True)
    def display_identity(self, instance: Deposit):
        return (
            instance.sys_no,
            _("客户 %(uid)s") % {"uid": instance.customer.uid},
        )

    @display(
        description=_("状态"),
        ordering="transfer__status",
        label={
            "confirming": "info",
            "confirmed": "success",
        },
    )
    def display_status(self, instance: Deposit):
        # 充值状态与 Transfer 同步，直接展示链上转账状态。
        return (instance.transfer.status, instance.transfer.get_status_display())

    @display(description=_("状态"))
    def display_status_text(self, instance: Deposit):
        # 详情页用纯文本：带 label 的 display 只在列表页渲染成标签。
        return instance.transfer.get_status_display()

    @display(description=_("项目"), ordering="customer__project__name")
    def display_project(self, instance: Deposit):
        return instance.customer.project

    @display(description=_("网络"), ordering="transfer__chain__code")
    def display_network(self, instance: Deposit):
        return fmt.stacked(
            instance.transfer.chain.code, instance.transfer.crypto.symbol
        )

    @display(description=_("到账数量"), ordering="transfer__amount")
    def display_amount(self, instance: Deposit):
        return fmt.number(
            instance.transfer.amount, unit=instance.transfer.crypto.symbol
        )

    @display(description=_("价值"), ordering="worth")
    def display_worth(self, instance: Deposit):
        return fmt.usd(instance.worth)

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
    def display_risk(self, instance: Deposit):
        assessment = transfer_assessment(instance.transfer)
        if assessment is None or not assessment.risk_level:
            return None
        return (
            assessment.risk_level,
            f"{assessment.risk_level} · {assessment.risk_score}",
        )

    @display(description=_("风险等级"))
    def display_risk_level(self, instance: Deposit):
        assessment = transfer_assessment(instance.transfer)
        return (assessment and assessment.risk_level) or fmt.empty()

    @display(description=_("风险分数"))
    def display_risk_score(self, instance: Deposit):
        assessment = transfer_assessment(instance.transfer)
        if assessment is None or assessment.risk_score is None:
            return fmt.empty()
        return assessment.risk_score

    @display(description=_("交易哈希"))
    def display_tx_hash(self, instance: Deposit):
        return fmt.mono(instance.transfer.hash)

    @display(description=_("创建时间"), ordering="created_at")
    def display_created_at(self, instance: Deposit):
        # Deposit.created_at 是无 verbose_name 的 auto_now_add 字段，
        # 默认列头会渲染成英文 "Created at"，这里显式给中文列名。
        return instance.created_at

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "customer__project",
                "transfer__chain",
                "transfer__crypto",
                "transfer__aml_assessment",
            )
        )

    @admin.action(description=_("重调度 ERC20 归集"))
    def reschedule_erc20_collect(self, request, queryset):
        success_count = 0
        skipped_count = 0
        failed_count = 0

        for deposit in queryset:
            try:
                scheduled = DepositService.schedule_collect_for_completed_deposit(
                    deposit
                )
            except DepositStatusError:
                skipped_count += 1
            except Exception:  # noqa
                failed_count += 1
                logger.exception("后台重调度 VaultSlot 归集失败", deposit_id=deposit.pk)
            else:
                if scheduled:
                    success_count += 1
                else:
                    skipped_count += 1

        level = messages.ERROR if failed_count else messages.SUCCESS
        self.message_user(
            request,
            _(
                "ERC20 归集重调度完成：成功 %(success)d，跳过 %(skipped)d，失败 %(failed)d"
            )
            % {
                "success": success_count,
                "skipped": skipped_count,
                "failed": failed_count,
            },
            level=level,
        )
