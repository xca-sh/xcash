import structlog
from django.contrib import admin
from django.contrib import messages
from django.contrib.admin.models import CHANGE
from django.contrib.admin.models import LogEntry
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _
from unfold.decorators import display

from chains.models import TxTask
from chains.models import TxTaskStatus
from common.admin import ReadOnlyModelAdmin
from common.admin_scan_cursor import SyncScanCursorToLatestActionMixin
from evm.models import EvmScanCursor
from evm.models import EvmTxTask

logger = structlog.get_logger()


@admin.register(EvmTxTask)
class EvmTxTaskAdmin(ReadOnlyModelAdmin):
    actions = ("mark_queued_failed_after_nonce_handled",)
    ordering = ("-created_at",)
    exclude = ("signed_payload",)
    readonly_fields = (
        "base_task",
        "sender",
        "chain",
        "nonce",
        "to",
        "value",
        "data",
        "gas",
        "gas_price",
        "formatted_last_attempt_at",
        "created_at",
    )
    list_display = (
        "display_sender",
        "display_chain",
        "tx_type",
        "to",
        "value",
        "display_nonce",
        "display_status",
        "created_at",
        "formatted_last_attempt_at",
    )
    # 状态展示优先读取统一父任务，后台查询一并预加载，避免 N+1。
    list_select_related = ("base_task", "sender", "chain")
    search_fields = ("base_task__tx_hash", "sender__address", "to")

    @admin.display(ordering="last_attempt_at", description=_("执行时间"))
    def formatted_last_attempt_at(self, obj: EvmTxTask):
        if obj.last_attempt_at:
            return date_format(timezone.localtime(obj.last_attempt_at), "DATETIME_FORMAT")
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
            messages.WARNING
            if (skipped_count or blocked_count)
            else messages.SUCCESS
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
        label={
            TxTaskStatus.QUEUED: "warning",
            TxTaskStatus.SUBMITTED: "warning",
            TxTaskStatus.SUCCEEDED: "success",
            TxTaskStatus.FAILED: "danger",
        },
    )
    def display_status(self, instance: EvmTxTask):
        return (instance.base_task.status, instance.status)

    @admin.display(description=_("类型"), ordering="base_task__tx_type")
    def tx_type(self, obj: EvmTxTask):  # pragma: no cover
        return obj.base_task.get_tx_type_display() if obj.base_task_id else "—"

    @admin.display(ordering="sender__address", description=_("发送地址"))
    def display_sender(self, obj: EvmTxTask):  # pragma: no cover
        return obj.sender

    @admin.display(ordering="chain__code", description=_("网络"))
    def display_chain(self, obj: EvmTxTask):  # pragma: no cover
        return obj.chain

    @admin.display(ordering="nonce", description="Nonce")
    def display_nonce(self, obj: EvmTxTask):  # pragma: no cover
        return obj.nonce


@admin.register(EvmScanCursor)
class EvmScanCursorAdmin(SyncScanCursorToLatestActionMixin, ReadOnlyModelAdmin):
    # 自扫描游标只承担观测与排障职责；后台统一只读展示，避免人工改游标破坏扫描连续性。
    actions = (
        "enable_selected_scanners",
        "disable_selected_scanners",
        "sync_selected_to_latest",
    )
    ordering = ("chain__code",)
    list_display = (
        "display_chain",
        "display_enabled",
        "display_lag_state",
        "display_chain_latest_block",
        "last_scanned_block",
        "display_scan_gap",
        "display_error_state",
        "display_error_summary",
        "updated_at",
    )
    list_filter = ("enabled", "chain")
    search_fields = ("chain__code", "last_error")
    list_select_related = ("chain",)
    readonly_fields = (
        "chain",
        "display_enabled",
        "last_scanned_block",
        "display_chain_latest_block",
        "display_scan_gap",
        "display_lag_state",
        "last_error",
        "last_error_at",
        "updated_at",
        "created_at",
    )
    fields = readonly_fields

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(ordering="chain__code", description=_("网络"))
    def display_chain(self, obj: EvmScanCursor):  # pragma: no cover
        return obj.chain

    @display(
        description=_("启用"),
        label={
            "yes": "success",
            "no": "danger",
        },
    )
    def display_enabled(self, obj: EvmScanCursor) -> str:
        return ("yes", _("是")) if obj.enabled else ("no", _("否"))

    @admin.display(description=_("链上最新块"))
    def display_chain_latest_block(self, obj: EvmScanCursor) -> int:  # pragma: no cover
        return obj.chain.latest_block_number

    @display(
        description=_("扫描状态"),
        label={
            "normal": "success",
            "error": "danger",
        },
    )
    def display_error_state(self, obj: EvmScanCursor) -> str:
        return ("error", _("异常")) if obj.last_error else ("normal", _("正常"))

    @admin.display(description=_("落后区块"))
    def display_scan_gap(self, obj: EvmScanCursor) -> int:
        # 以链上当前最新高度对比主扫描游标，便于快速判断该链是否积压。
        return max(obj.chain.latest_block_number - obj.last_scanned_block, 0)

    @display(
        description=_("积压"),
        label={
            "normal": "success",
            "minor": "warning",
            "severe": "danger",
        },
    )
    def display_lag_state(self, obj: EvmScanCursor) -> str:
        gap = self.display_scan_gap(obj)
        if gap >= 128:
            return ("severe", _("严重"))
        if gap >= 16:
            return ("minor", _("轻微"))
        return ("normal", _("正常"))

    @admin.display(description=_("错误摘要"))
    def display_error_summary(self, obj: EvmScanCursor) -> str:
        if not obj.last_error:
            return "—"
        # 列表页只展示摘要，详情页保留完整 last_error 原文。
        return obj.last_error[:60]
