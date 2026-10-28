import structlog
from django.contrib import admin
from django.contrib import messages
from django.contrib.admin.models import CHANGE
from django.contrib.admin.models import LogEntry
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _
from unfold.contrib.filters.admin import ChoicesDropdownFilter
from unfold.contrib.filters.admin import RangeDateTimeFilter
from unfold.contrib.filters.admin import RelatedDropdownFilter
from unfold.decorators import display

from chains.admin import TX_TASK_STATUS_LABELS
from chains.models import TxTask
from chains.models import TxTaskStatus
from common import admin_display as fmt
from common.admin import ReadOnlyModelAdmin
from evm.models import EvmTxTask

logger = structlog.get_logger()


@admin.register(EvmTxTask)
class EvmTxTaskAdmin(ReadOnlyModelAdmin):
    actions = ("mark_queued_failed_after_nonce_handled",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    exclude = ("signed_payload",)
    readonly_fields = (
        "base_task",
        "display_full_sender",
        "chain",
        "display_tx_type_text",
        "display_status_text",
        "nonce",
        "display_full_to",
        "value",
        "display_data",
        "gas",
        "gas_price",
        "formatted_last_attempt_at",
        "created_at",
    )
    list_display = (
        "display_nonce",
        "display_chain",
        "display_tx_type",
        "display_sender",
        "display_to",
        "display_status",
        "created_at",
        "formatted_last_attempt_at",
    )
    list_filter = (
        ("base_task__status", ChoicesDropdownFilter),
        ("base_task__tx_type", ChoicesDropdownFilter),
        ("chain", RelatedDropdownFilter),
        ("created_at", RangeDateTimeFilter),
    )
    # 状态展示优先读取统一父任务，后台查询一并预加载，避免 N+1。
    list_select_related = ("base_task", "sender", "chain")
    search_fields = ("base_task__tx_hash", "sender__address", "to")
    search_help_text = _("支持按交易哈希、发送地址或目标地址搜索")
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
            _("交易参数"),
            {
                "classes": ("tab",),
                "fields": (
                    "display_full_sender",
                    "nonce",
                    "display_full_to",
                    "value",
                    "gas",
                    "gas_price",
                    "display_data",
                ),
                "description": _(
                    "签名产物 signed_payload 不在后台展示：它可被任何人直接广播，等同一次性支付凭证。"
                ),
            },
        ),
    )

    @admin.display(ordering="last_attempt_at", description=_("执行时间"))
    def formatted_last_attempt_at(self, obj: EvmTxTask):
        if obj.last_attempt_at:
            return date_format(
                timezone.localtime(obj.last_attempt_at), "DATETIME_FORMAT"
            )
        return None

    def has_mark_queued_failed_permission(self, request):
        # 标记 QUEUED 任务失败会解除 nonce 队列阻塞、放行后续 nonce，属资金调度治理
        # 操作。ReadOnlyModelAdmin 已禁 change/add/delete，view 是所有查看者的基线
        # 权限；若靠 view 放行等于把动队列的动作开放给只读审计员，故收口到超管，与
        # chains.requeue / SystemSettings 等系统级治理入口口径一致。
        return bool(request.user.is_active and request.user.is_superuser)

    @admin.action(
        description=_("确认 nonce 已处理后标记 QUEUED 任务失败"),
        permissions=["mark_queued_failed"],
    )
    def mark_queued_failed_after_nonce_handled(self, request, queryset):
        updated_count = 0
        skipped_count = 0
        blocked_count = 0
        # 同一 (chain, sender) 的链上 nonce 只查一次，避免逐任务重复打 RPC。
        nonce_cache: dict[tuple[int, str], int | None] = {}
        for task in queryset.select_related("base_task", "sender", "chain"):
            if task.base_task.status != TxTaskStatus.QUEUED:
                skipped_count += 1
                continue
            if not self.sender_nonce_consumed(task=task, nonce_cache=nonce_cache):
                # 链上 nonce 尚未越过该任务、或查询失败：拦截。否则标记失败会放行更高
                # nonce，而被跳过的 nonce 永不被消费，该发送地址后续交易永久卡死。
                blocked_count += 1
                continue
            if TxTask.mark_finalized_failed(
                task_id=task.base_task_id,
                expected_status=TxTaskStatus.QUEUED,
            ):
                self.log_mark_queued_failed(request=request, task=task)
                updated_count += 1
            else:
                skipped_count += 1

        level = (
            messages.WARNING if (skipped_count or blocked_count) else messages.SUCCESS
        )
        self.message_user(
            request,
            _(
                "已标记 %(updated)d 个 QUEUED 任务为失败，跳过 %(skipped)d 个非 QUEUED "
                "任务，拦截 %(blocked)d 个链上 nonce 尚未消费或查询失败的任务。"
            )
            % {
                "updated": updated_count,
                "skipped": skipped_count,
                "blocked": blocked_count,
            },
            level=level,
        )

    @staticmethod
    def sender_nonce_consumed(
        *, task: EvmTxTask, nonce_cache: dict[tuple[int, str], int | None]
    ) -> bool:
        """判断该任务 nonce 是否已被链上消费（发送地址的链上 nonce 已越过它）。

        查询失败按"未消费"处理（返回 False），宁可拦截也不放行造成 nonce 缺口。
        """
        key = (task.chain_id, task.sender.address)
        if key not in nonce_cache:
            try:
                nonce_cache[key] = int(
                    task.chain.w3.eth.get_transaction_count(task.sender.address)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "标记 QUEUED 失败前查询链上 nonce 失败，已拦截",
                    evm_task_id=task.pk,
                    chain=task.chain.code,
                    sender=task.sender.address,
                    error=str(exc),
                )
                nonce_cache[key] = None
        on_chain_next_nonce = nonce_cache[key]
        if on_chain_next_nonce is None:
            return False
        return on_chain_next_nonce > task.nonce

    @staticmethod
    def log_mark_queued_failed(*, request, task: EvmTxTask) -> None:
        """把人工标记失败写入 admin LogEntry，保留操作者、时间与前后语义可追溯。"""
        LogEntry.objects.log_actions(
            request.user.pk,
            [task],
            CHANGE,
            change_message=(
                "人工标记 QUEUED 任务失败（已确认链上 nonce 消费）："
                f"chain={task.chain.code} sender={task.sender.address} "
                f"nonce={task.nonce} tx_task_id={task.base_task_id}"
            ),
            single_object=True,
        )

    @display(
        description=_("状态"),
        ordering="base_task__status",
        label=TX_TASK_STATUS_LABELS,
    )
    def display_status(self, instance: EvmTxTask):
        return (instance.base_task.status, instance.status)

    @display(description=_("类型"), ordering="base_task__tx_type", label=True)
    def display_tx_type(self, obj: EvmTxTask):  # pragma: no cover
        return obj.base_task.get_tx_type_display() if obj.base_task_id else fmt.empty()

    @display(description=_("类型"))
    def display_tx_type_text(self, obj: EvmTxTask):  # pragma: no cover
        # 详情页不能复用带 label 的 display：unfold 的标签渲染只作用于列表页，
        # 放进 fieldsets 会把 (value, text) 元组原样打印出来。
        return obj.base_task.get_tx_type_display() if obj.base_task_id else fmt.empty()

    @display(description=_("状态"))
    def display_status_text(self, obj: EvmTxTask):  # pragma: no cover
        return obj.status

    @display(description=_("网络"), ordering="chain__code")
    def display_chain(self, obj: EvmTxTask):  # pragma: no cover
        return obj.chain

    @display(description=_("发送地址"), ordering="sender__address")
    def display_sender(self, obj: EvmTxTask):  # pragma: no cover
        return fmt.truncated(obj.sender.address)

    @display(description=_("发送地址"))
    def display_full_sender(self, obj: EvmTxTask):  # pragma: no cover
        return fmt.mono(obj.sender.address)

    @display(description=_("目标地址"), ordering="to")
    def display_to(self, obj: EvmTxTask):  # pragma: no cover
        return fmt.truncated(obj.to)

    @display(description=_("目标地址"))
    def display_full_to(self, obj: EvmTxTask):  # pragma: no cover
        return fmt.mono(obj.to)

    @display(description="Calldata")
    def display_data(self, obj: EvmTxTask):  # pragma: no cover
        return fmt.scroll_box(obj.data)

    @display(description="Nonce", ordering="nonce")
    def display_nonce(self, obj: EvmTxTask):  # pragma: no cover
        return fmt.number(obj.nonce)
