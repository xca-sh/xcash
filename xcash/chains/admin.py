from django import forms
from django.contrib import admin
from django.contrib import messages
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.contrib.filters.admin import ChoicesDropdownFilter
from unfold.contrib.filters.admin import RangeDateTimeFilter
from unfold.contrib.filters.admin import RelatedDropdownFilter
from unfold.decorators import display

from chains.models import Address
from chains.models import Chain
from chains.models import ChainType
from chains.models import DepositVaultSlot
from chains.models import InvoiceVaultSlot
from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import TransferType
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import VaultSlotBalance
from chains.models import VaultSlotCollectSchedule
from chains.models import VaultSlotUsage
from chains.models import Wallet
from common import admin_display as fmt
from common.admin import ModelAdmin
from common.admin import ReadOnlyModelAdmin
from common.admin import TabularInline
from common.admin_scan_cursor import SCAN_LAG_LABELS
from common.admin_scan_cursor import scan_lag_state

# Register your models here.

# 上链任务状态在 TxTask / EvmTxTask / TronTxTask 三处后台展示，
# 集中定义配色，避免同一状态在不同页面出现不同颜色。
TX_TASK_STATUS_LABELS = {
    TxTaskStatus.QUEUED: "warning",
    TxTaskStatus.SUBMITTED: "info",
    TxTaskStatus.SUCCEEDED: "success",
    TxTaskStatus.FAILED: "danger",
}


class ChainAdminForm(forms.ModelForm):
    class Meta:
        model = Chain
        fields = "__all__"  # noqa: DJ007


# 各链族扫描游标挂在 Chain 上的反向关系名与后台详情路由。按字符串引用，
# 保持 chains 不 import evm / tron（分层契约见 pyproject.toml 的 [tool.importlinter]）。
SCAN_CURSOR_RELATIONS = {
    ChainType.EVM: ("evm_scan_cursors", "admin:evm_evmscancursor_change"),
    ChainType.TRON: ("tron_watch_cursors", "admin:tron_tronwatchcursor_change"),
}

SCANNER_STATE_LABELS = {
    **SCAN_LAG_LABELS,
    "missing": "info",
    "paused": "warning",
    "error": "danger",
}


@admin.register(Chain)
class ChainAdmin(ModelAdmin):
    form = ChainAdminForm
    ordering = ("is_testnet", "sort_order", "code")
    list_display = (
        "display_identity",
        "native_coin_display",
        "display_latest_block",
        "display_scanner_state",
        "display_scanned_block",
        "display_scan_gap",
        "active",
    )
    list_editable = ("active",)
    list_filter = ("active", "is_testnet")
    search_fields = ("code",)
    search_help_text = _("支持按链代码搜索")
    readonly_fields = (
        "display_scanner_state",
        "display_scanned_block",
        "display_latest_block",
        "display_scan_gap",
        "display_scanner_error",
        "display_scanner_error_at",
        "display_scanner_link",
    )

    def get_queryset(self, request):
        # 列表页每行都要读扫描游标，预取避免 N+1。
        return (
            super()
            .get_queryset(request)
            .prefetch_related(
                *(relation for relation, _url in SCAN_CURSOR_RELATIONS.values())
            )
        )

    @staticmethod
    def scan_cursor(obj: Chain):
        """返回本链的扫描游标；每条链至多一个，尚未初始化时为 None。"""
        relation = SCAN_CURSOR_RELATIONS.get(obj.type)
        if relation is None or obj.pk is None:
            return None
        # 走 .all() 才能命中 get_queryset 的预取缓存。
        return next(iter(getattr(obj, relation[0]).all()), None)

    def scan_gap(self, obj: Chain) -> int | None:
        cursor = self.scan_cursor(obj)
        if cursor is None:
            return None
        return max(obj.latest_block_number - cursor.last_scanned_block, 0)

    @display(description=_("链"), ordering="code", header=True)
    def display_identity(self, obj: Chain) -> tuple:
        return (obj.name, obj.code)

    @display(description=_("原生币"))
    def native_coin_display(self, obj: Chain) -> str:
        return obj.spec.native_coin_symbol

    @display(description=_("最新区块"), ordering="latest_block_number")
    def display_latest_block(self, obj: Chain):
        return fmt.number(obj.latest_block_number)

    @display(description=_("扫描器"), label=SCANNER_STATE_LABELS)
    def display_scanner_state(self, obj: Chain) -> tuple:
        # 优先级：未初始化 > 已暂停 > 最近报错 > 积压级别，取最需要运维关注的一项。
        cursor = self.scan_cursor(obj)
        if cursor is None:
            return ("missing", _("未初始化"))
        if not cursor.enabled:
            return ("paused", _("已暂停"))
        if cursor.last_error:
            return ("error", _("异常"))
        return scan_lag_state(self.scan_gap(obj))

    @display(description=_("已扫描到"))
    def display_scanned_block(self, obj: Chain):
        cursor = self.scan_cursor(obj)
        return fmt.number(cursor.last_scanned_block if cursor else None)

    @display(description=_("落后区块"))
    def display_scan_gap(self, obj: Chain):
        return fmt.number(self.scan_gap(obj))

    @display(description=_("最近错误"))
    def display_scanner_error(self, obj: Chain):
        cursor = self.scan_cursor(obj)
        return fmt.scroll_box(cursor.last_error if cursor else None)

    @display(description=_("最近错误时间"))
    def display_scanner_error_at(self, obj: Chain):
        cursor = self.scan_cursor(obj)
        if cursor is None or cursor.last_error_at is None:
            return fmt.empty()
        return cursor.last_error_at

    @display(description=_("游标管理"))
    def display_scanner_link(self, obj: Chain):
        cursor = self.scan_cursor(obj)
        if cursor is None:
            return fmt.empty()
        url = reverse(SCAN_CURSOR_RELATIONS[obj.type][1], args=[cursor.pk])
        return format_html(
            '<a class="text-primary-600 dark:text-primary-400" href="{}">{}</a>',
            url,
            _("启停 / 追平扫描游标"),
        )

    base_fieldsets = (
        (
            _("基本信息"),
            {
                "classes": ("tab",),
                "fields": (
                    "code",
                    "sort_order",
                    "active",
                ),
                "description": _(
                    "链类型、原生币与区块确认数由链代码对应的内置 spec 决定，不在后台维护。"
                ),
            },
        ),
    )
    evm_fieldsets = (
        (
            "EVM",
            {
                "classes": ("tab",),
                "fields": (
                    "rpc",
                    "evm_log_max_block_range",
                ),
            },
        ),
    )
    tron_fieldsets = (
        (
            "Tron",
            {
                "classes": ("tab",),
                "fields": ("tron_api_key",),
            },
        ),
    )
    scanner_fieldsets = (
        (
            _("扫描器"),
            {
                "classes": ("tab",),
                "fields": (
                    "display_scanner_state",
                    "display_scanned_block",
                    "display_latest_block",
                    "display_scan_gap",
                    "display_scanner_error",
                    "display_scanner_error_at",
                    "display_scanner_link",
                ),
            },
        ),
    )

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return (
                *self.base_fieldsets,
                *self.evm_fieldsets,
                *self.tron_fieldsets,
            )
        if obj.type == ChainType.EVM:
            return (*self.base_fieldsets, *self.evm_fieldsets, *self.scanner_fieldsets)
        if obj.type == ChainType.TRON:
            return (
                *self.base_fieldsets,
                *self.tron_fieldsets,
                *self.scanner_fieldsets,
            )
        return self.base_fieldsets


@admin.register(Wallet)
class WalletAdmin(ReadOnlyModelAdmin):
    # 助记词密文永远不进后台；这里只暴露钱包与其派生地址的对应关系。
    list_display = ("__str__", "display_address_count")
    fields = ("__str__",)

    def get_queryset(self, request):
        from django.db.models import Count

        return super().get_queryset(request).annotate(address_total=Count("address"))

    @display(description=_("派生地址数"), ordering="address_total")
    def display_address_count(self, obj: Wallet):
        return fmt.number(obj.address_total)


@admin.register(Address)
class AddressAdmin(ReadOnlyModelAdmin):
    ordering = ("-created_at",)
    list_select_related = ("wallet",)
    list_display = (
        "display_address",
        "display_usage",
        "chain_type",
        "bip44_account",
        "address_index",
        "created_at",
    )
    list_filter = (
        ("usage", ChoicesDropdownFilter),
        ("chain_type", ChoicesDropdownFilter),
    )
    search_fields = ("address",)
    search_help_text = _("支持按地址搜索")
    readonly_fields = (
        "display_full_address",
        "wallet",
        "usage",
        "chain_type",
        "bip44_account",
        "address_index",
        "created_at",
    )
    fields = readonly_fields

    @display(description=_("地址"), ordering="address")
    def display_address(self, obj: Address):
        return fmt.truncated(obj.address)

    @display(description=_("地址"))
    def display_full_address(self, obj: Address):
        return fmt.mono(obj.address)

    @display(description=_("用途"), ordering="usage", label=True)
    def display_usage(self, obj: Address):
        return obj.get_usage_display()


@admin.register(Transfer)
class TransferAdmin(ReadOnlyModelAdmin):
    date_hierarchy = "datetime"
    ordering = ("-timestamp",)
    list_select_related = ("chain", "crypto")
    list_filter_sheet = False
    search_fields = ("hash", "from_address", "to_address")
    search_help_text = _("支持按交易哈希、付款地址或收款地址搜索")
    list_filter = (
        ("status", ChoicesDropdownFilter),
        ("type", ChoicesDropdownFilter),
        ("chain", RelatedDropdownFilter),
        ("crypto", RelatedDropdownFilter),
        ("datetime", RangeDateTimeFilter),
    )
    readonly_fields = (
        "display_crypto",
        "display_chain",
        "display_full_hash",
        "display_full_block_hash",
        "display_from_address",
        "display_to_address",
        "display_amount",
    )

    list_display = (
        "display_hash",
        "display_route",
        "display_amount",
        "display_chain",
        "display_type",
        "display_status",
        "block",
        "datetime",
    )

    fieldsets = (
        (
            _("转账"),
            {
                "classes": ("tab",),
                "fields": (
                    "display_full_hash",
                    "display_chain",
                    "display_crypto",
                    "display_amount",
                    "value",
                    "type",
                    "status",
                    "confirm_mode",
                ),
            },
        ),
        (
            _("收付双方"),
            {
                "classes": ("tab",),
                "fields": (
                    "display_from_address",
                    "display_to_address",
                ),
            },
        ),
        (
            _("链上位置"),
            {
                "classes": ("tab",),
                "fields": (
                    "block",
                    "display_full_block_hash",
                    "event_index",
                    "datetime",
                    "timestamp",
                    "processed_at",
                ),
            },
        ),
    )

    @display(description=_("交易哈希"), ordering="hash")
    def display_hash(self, obj: Transfer):
        return fmt.truncated(obj.hash)

    @display(description=_("付款 → 收款"))
    def display_route(self, obj: Transfer):
        # 付款方与收款方成对阅读才有意义，合并成一列的两行，省掉一列横向空间。
        return fmt.stacked(
            fmt.truncated(obj.from_address),
            fmt.truncated(obj.to_address),
        )

    @display(description=_("数量"), ordering="amount")
    def display_amount(self, obj: Transfer):
        return fmt.number(obj.amount, unit=obj.crypto.symbol)

    @display(description=_("加密货币"))
    def display_crypto(self, obj: Transfer):
        return obj.crypto.symbol

    @display(description=_("链"), ordering="chain__code")
    def display_chain(self, obj: Transfer):
        return obj.chain.name

    @display(description=_("付款地址"))
    def display_from_address(self, obj: Transfer):
        return fmt.mono(obj.from_address)

    @display(description=_("收款地址"))
    def display_to_address(self, obj: Transfer):
        return fmt.mono(obj.to_address)

    @display(description=_("交易哈希"))
    def display_full_hash(self, obj: Transfer):
        return fmt.mono(obj.hash)

    @display(description=_("区块哈希"))
    def display_full_block_hash(self, obj: Transfer):
        return fmt.mono(obj.block_hash)

    @display(
        description=_("归属"),
        ordering="type",
        label={
            TransferType.Unmatched: "",
            TransferType.Invoice: "primary",
            TransferType.Deposit: "info",
        },
    )
    def display_type(self, obj: Transfer):
        return (obj.type, obj.get_type_display())

    @display(
        description=_("状态"),
        ordering="status",
        label={
            TransferStatus.CONFIRMING: "info",
            TransferStatus.CONFIRMED: "success",
        },
    )
    def display_status(self, instance: Transfer):
        return (instance.status, instance.get_status_display())


@admin.register(TxTask)
class TxTaskAdmin(ReadOnlyModelAdmin):
    # TxTask 是跨链统一锚点；后台只做观察与排障，禁止人工修改，避免写入非法的 status 状态。
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_display = (
        "display_tx_hash",
        "display_tx_type",
        "display_chain",
        "display_sender",
        "display_status",
        "created_at",
        "updated_at",
    )
    list_filter = (
        ("status", ChoicesDropdownFilter),
        ("tx_type", ChoicesDropdownFilter),
        ("chain", RelatedDropdownFilter),
        ("created_at", RangeDateTimeFilter),
    )
    list_select_related = ("sender", "chain")
    search_fields = ("tx_hash", "sender__address")
    search_help_text = _("支持按交易哈希或发送地址搜索")
    readonly_fields = (
        "display_full_tx_hash",
        "display_chain",
        "display_full_sender",
        "tx_type",
        "status",
        "created_at",
        "updated_at",
    )
    fields = readonly_fields

    @display(description=_("发送地址"), ordering="sender__address")
    def display_sender(self, obj: TxTask):
        return fmt.truncated(obj.sender.address)

    @display(description=_("发送地址"))
    def display_full_sender(self, obj: TxTask):
        return fmt.mono(obj.sender.address)

    @display(description=_("网络"), ordering="chain__code")
    def display_chain(self, obj: TxTask):
        return obj.chain

    @display(description=_("类型"), ordering="tx_type", label=True)
    def display_tx_type(self, obj: TxTask):
        return obj.get_tx_type_display()

    @display(description=_("交易哈希"), ordering="tx_hash")
    def display_tx_hash(self, obj: TxTask):
        return fmt.truncated(obj.tx_hash)

    @display(description=_("交易哈希"))
    def display_full_tx_hash(self, obj: TxTask):
        return fmt.mono(obj.tx_hash)

    @display(description=_("状态"), ordering="status", label=TX_TASK_STATUS_LABELS)
    def display_status(self, instance: TxTask):
        # TxTask.display_status 直接取单枚举 status 的展示文案，
        # 这里沿用同一来源避免后台与业务代码的展示口径漂移。
        return (instance.status, instance.display_status)


class VaultSlotCollectScheduleInline(TabularInline):
    model = VaultSlotCollectSchedule
    extra = 0
    can_delete = False
    tab = True
    show_count = True
    show_title = False
    per_page = 10
    verbose_name = _("归集计划")
    verbose_name_plural = _("归集计划")
    fields = ("crypto", "due_at", "tx_task", "created_at", "updated_at")
    readonly_fields = fields
    ordering = ("-due_at",)

    def has_add_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("crypto", "tx_task")


class VaultSlotBalanceInline(TabularInline):
    model = VaultSlotBalance
    extra = 0
    can_delete = False
    tab = True
    show_count = True
    show_title = False
    verbose_name = _("余额快照")
    verbose_name_plural = _("余额快照")
    fields = (
        "crypto",
        "amount",
        "worth",
        "synced_block_number",
        "synced_at",
        "updated_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("crypto")


class VaultSlotAdminBase(ReadOnlyModelAdmin):
    inlines = (VaultSlotBalanceInline, VaultSlotCollectScheduleInline)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_filter = (
        ("chain", RelatedDropdownFilter),
        "is_deployed",
        "has_received",
        ("created_at", RangeDateTimeFilter),
    )
    readonly_fields = (
        "project",
        "customer",
        "invoice_index",
        "chain",
        "display_full_address",
        "display_salt",
        "deploy_tx_task",
        "is_deployed",
        "has_received",
        "created_at",
    )
    usage = None

    @display(description=_("收款地址"), ordering="address")
    def display_address(self, obj):
        return fmt.truncated(obj.address)

    @display(description=_("收款地址"))
    def display_full_address(self, obj):
        return fmt.mono(obj.address)

    @display(description="CREATE2 Salt")
    def display_salt(self, obj):
        # salt 是 BinaryField，直接渲染会得到 memoryview 字面量，统一转成十六进制。
        return fmt.mono(bytes(obj.salt).hex()) if obj.salt else fmt.empty()

    @display(
        description=_("部署"),
        ordering="is_deployed",
        label={"deployed": "success", "pending": "warning"},
    )
    def display_deploy_state(self, obj):
        return (
            ("deployed", _("已部署")) if obj.is_deployed else ("pending", _("未部署"))
        )

    @display(
        description=_("收款"),
        ordering="has_received",
        label={"received": "success", "empty": ""},
    )
    def display_received_state(self, obj):
        return ("received", _("有过入账")) if obj.has_received else ("empty", _("空"))

    def get_queryset(self, request):
        qs = (
            super()
            .get_queryset(request)
            .select_related("chain", "customer", "project", "deploy_tx_task")
        )
        return qs.filter(usage=self.usage)


@admin.register(DepositVaultSlot)
class DepositVaultSlotAdmin(VaultSlotAdminBase):
    list_display = (
        "display_address",
        "customer",
        "project",
        "chain",
        "display_deploy_state",
        "display_received_state",
        "created_at",
    )
    search_fields = ("customer__uid", "project__name", "address")
    search_help_text = _("支持按客户 UID、项目名称或合约地址搜索")
    usage = VaultSlotUsage.DEPOSIT
    fieldsets = (
        (
            _("归属"),
            {
                "classes": ("tab",),
                "fields": ("project", "customer", "chain", "created_at"),
            },
        ),
        (
            _("合约"),
            {
                "classes": ("tab",),
                "fields": (
                    "display_full_address",
                    "display_salt",
                    "is_deployed",
                    "has_received",
                    "deploy_tx_task",
                ),
            },
        ),
    )


@admin.register(InvoiceVaultSlot)
class InvoiceVaultSlotAdmin(VaultSlotAdminBase):
    list_display = (
        "display_address",
        "project",
        "invoice_index",
        "chain",
        "display_deploy_state",
        "display_received_state",
        "created_at",
    )
    search_fields = ("project__name", "address")
    search_help_text = _("支持按项目名称或合约地址搜索")
    usage = VaultSlotUsage.INVOICE
    fieldsets = (
        (
            _("归属"),
            {
                "classes": ("tab",),
                "fields": ("project", "invoice_index", "chain", "created_at"),
            },
        ),
        (
            _("合约"),
            {
                "classes": ("tab",),
                "fields": (
                    "display_full_address",
                    "display_salt",
                    "is_deployed",
                    "has_received",
                    "deploy_tx_task",
                ),
            },
        ),
    )


@admin.register(VaultSlotCollectSchedule)
class VaultSlotCollectScheduleAdmin(ReadOnlyModelAdmin):
    actions = ("requeue_failed_collect_schedules",)
    date_hierarchy = "due_at"
    ordering = ("due_at",)
    list_display = (
        "display_slot_address",
        "chain",
        "crypto",
        "due_at",
        "display_progress",
        "created_at",
    )
    list_filter = (
        ("chain", RelatedDropdownFilter),
        ("crypto", RelatedDropdownFilter),
        ("due_at", RangeDateTimeFilter),
    )
    search_fields = ("vault_slot__address", "tx_task__tx_hash")
    search_help_text = _("支持按收款合约地址或归集交易哈希搜索")
    list_select_related = ("vault_slot", "chain", "crypto", "tx_task")
    readonly_fields = (
        "chain",
        "vault_slot",
        "crypto",
        "due_at",
        "tx_task",
        "display_progress_text",
        "created_at",
        "updated_at",
    )
    fields = readonly_fields

    @display(description=_("收款合约"), ordering="vault_slot__address")
    def display_slot_address(self, obj: VaultSlotCollectSchedule):
        return fmt.truncated(obj.vault_slot.address)

    @display(
        description=_("进度"),
        label={
            "pending": "warning",
            "queued": "warning",
            "submitted": "info",
            "succeeded": "success",
            "failed": "danger",
        },
    )
    def display_progress(self, obj: VaultSlotCollectSchedule):
        # 归集计划本身没有状态字段：未派生 TxTask 即「等待窗口到期」，
        # 已派生则直接透传链上任务状态，避免运维再点进 TxTask 才知道结果。
        if obj.tx_task is None:
            return ("pending", _("待到期"))
        return (obj.tx_task.status, obj.tx_task.get_status_display())

    @display(description=_("进度"))
    def display_progress_text(self, obj: VaultSlotCollectSchedule):
        # 详情页不能复用带 label 的 display：unfold 的标签渲染只作用于列表页，
        # 放进 fieldsets 会把 (value, text) 元组原样打印出来。
        if obj.tx_task is None:
            return _("待到期")
        return obj.tx_task.get_status_display()

    def has_requeue_permission(self, request):
        # 重新排队失败归集会新建 pending 计划并触发链上归集交易、消耗热钱包 gas，
        # 属资金治理操作。ReadOnlyModelAdmin 已禁掉 change/add/delete，view 是所有
        # 查看者的基线权限；若用 view 放行等于把动钱动作开放给只读审计员，故收口到
        # 超管，与 SystemSettings / SystemWallet 等系统级治理入口口径一致。
        return bool(request.user.is_active and request.user.is_superuser)

    @admin.action(description=_("重新排队失败的归集计划"), permissions=["requeue"])
    def requeue_failed_collect_schedules(self, request, queryset):
        requeued_count = 0
        skipped_count = 0
        for schedule in queryset.select_related(
            "chain", "crypto", "vault_slot", "tx_task"
        ):
            pending_schedule = schedule.requeue_failed_collect()
            if pending_schedule is None:
                skipped_count += 1
                continue
            requeued_count += 1

        level = messages.WARNING if skipped_count else messages.SUCCESS
        self.message_user(
            request,
            _("已重新排队 %(requeued)d 个失败归集计划，跳过 %(skipped)d 个非失败计划。")
            % {"requeued": requeued_count, "skipped": skipped_count},
            level=level,
        )
