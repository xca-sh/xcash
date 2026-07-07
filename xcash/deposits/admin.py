import structlog
from django.contrib import admin
from django.contrib import messages
from django.utils.translation import gettext_lazy as _
from unfold.decorators import display

from common.admin import ReadOnlyModelAdmin
from deposits.exceptions import DepositStatusError
from deposits.models import Deposit
from deposits.service import DepositService

logger = structlog.get_logger()


@admin.register(Deposit)
class DepositAdmin(ReadOnlyModelAdmin):
    actions = ("reschedule_erc20_collect",)
    list_display = (
        "sys_no",
        "display_project",
        "customer",
        "display_chain",
        "display_crypto",
        "display_amount",
        "display_status",
        "risk_level",
        "risk_score",
        "created_at",
    )
    search_fields = ("sys_no", "customer__uid", "transfer__hash")
    list_filter = (
        "transfer__status",
        "risk_level",
        "transfer__crypto",
        "transfer__chain",
    )
    readonly_fields = (
        "sys_no",
        "customer",
        "transfer",
        "worth",
        "display_status",
        "risk_level",
        "risk_score",
        "created_at",
        "updated_at",
    )

    @display(
        description=_("状态"),
        label={
            "confirming": "info",
            "confirmed": "success",
        },
    )
    def display_status(self, instance: Deposit):
        # 充值状态与 Transfer 同步，直接展示链上转账状态。
        return (instance.transfer.status, instance.transfer.get_status_display())

    @display(description=_("项目"))
    def display_project(self, instance: Deposit):
        return instance.customer.project

    @display(description=_("链"))
    def display_chain(self, instance: Deposit):
        return instance.transfer.chain.code

    @display(description=_("币种"))
    def display_crypto(self, instance: Deposit):
        return instance.transfer.crypto.symbol

    @display(description=_("数量"))
    def display_amount(self, instance: Deposit):
        return instance.transfer.amount

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "customer__project",
                "transfer__chain",
                "transfer__crypto",
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
