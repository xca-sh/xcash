from django.contrib import admin
from django.contrib import messages
from django.contrib.auth import get_permission_codename
from django.utils.translation import gettext_lazy as _
from unfold.decorators import display

from chains.models import TxTask
from chains.models import TxTaskStatus
from common.admin import ReadOnlyModelAdmin
from common.admin_scan_cursor import SyncScanCursorToLatestActionMixin
from evm.models import EvmScanCursor
from evm.models import EvmTxTask


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

    def has_change_permission(self, request, obj=None):
        opts = self.opts
        codename = get_permission_codename("change", opts)
        return request.user.has_perm(f"{opts.app_label}.{codename}")

    @admin.display(ordering="last_attempt_at", description="执行时间")
    def formatted_last_attempt_at(self, obj: EvmTxTask):
        if obj.last_attempt_at:
            return obj.last_attempt_at.strftime("%-m月%-d日 %H:%M:%S")
        return None

    @admin.action(description="确认 nonce 已处理后标记 QUEUED 任务失败")
    def mark_queued_failed_after_nonce_handled(self, request, queryset):
        updated_count = 0
        skipped_count = 0
        for task in queryset.select_related("base_task"):
            if task.base_task.status != TxTaskStatus.QUEUED:
                skipped_count += 1
                continue
            if TxTask.mark_finalized_failed(
                task_id=task.base_task_id,
                expected_status=TxTaskStatus.QUEUED,
            ):
                updated_count += 1
            else:
                skipped_count += 1

        level = messages.WARNING if skipped_count else messages.SUCCESS
        self.message_user(
            request,
            _(
                "已标记 %(updated)d 个 QUEUED 任务为失败，跳过 %(skipped)d 个非 QUEUED 任务。"
            )
            % {"updated": updated_count, "skipped": skipped_count},
            level=level,
        )

    @display(
        description="状态",
        label={
            "待提交": "warning",
            "已提交": "warning",
            "成功": "success",
            "失败": "danger",
        },
    )
    def display_status(self, instance: EvmTxTask):
        return instance.status

    @admin.display(description="类型", ordering="base_task__tx_type")
    def tx_type(self, obj: EvmTxTask):  # pragma: no cover
        return obj.base_task.get_tx_type_display() if obj.base_task_id else "—"

    @admin.display(ordering="sender__address", description="发送地址")
    def display_sender(self, obj: EvmTxTask):  # pragma: no cover
        return obj.sender

    @admin.display(ordering="chain__code", description="网络")
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

    @admin.display(ordering="chain__code", description="网络")
    def display_chain(self, obj: EvmScanCursor):  # pragma: no cover
        return obj.chain

    @display(
        description="启用",
        label={
            "是": "success",
            "否": "danger",
        },
    )
    def display_enabled(self, obj: EvmScanCursor) -> str:
        return "是" if obj.enabled else "否"

    @admin.display(description="链上最新块")
    def display_chain_latest_block(self, obj: EvmScanCursor) -> int:  # pragma: no cover
        return obj.chain.latest_block_number

    @display(
        description="扫描状态",
        label={
            "正常": "success",
            "异常": "danger",
        },
    )
    def display_error_state(self, obj: EvmScanCursor) -> str:
        return "异常" if obj.last_error else "正常"

    @admin.display(description="落后区块")
    def display_scan_gap(self, obj: EvmScanCursor) -> int:
        # 以链上当前最新高度对比主扫描游标，便于快速判断该链是否积压。
        return max(obj.chain.latest_block_number - obj.last_scanned_block, 0)

    @display(
        description="积压",
        label={
            "正常": "success",
            "轻微": "warning",
            "严重": "danger",
        },
    )
    def display_lag_state(self, obj: EvmScanCursor) -> str:
        gap = self.display_scan_gap(obj)
        if gap >= 128:
            return "严重"
        if gap >= 16:
            return "轻微"
        return "正常"

    @admin.display(description="错误摘要")
    def display_error_summary(self, obj: EvmScanCursor) -> str:
        if not obj.last_error:
            return "—"
        # 列表页只展示摘要，详情页保留完整 last_error 原文。
        return obj.last_error[:60]
