from django.contrib import admin
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from unfold.contrib.filters.admin import ChoicesDropdownFilter
from unfold.contrib.filters.admin import RangeDateTimeFilter
from unfold.contrib.filters.admin import RelatedDropdownFilter
from unfold.decorators import display

from common import admin_display as fmt
from common.admin import ReadOnlyModelAdmin
from common.admin import TabularInline
from core.monitoring import OperationalRiskService
from webhooks.models import DeliveryAttempt
from webhooks.models import WebhookEvent

# Register your models here.


class EventAttentionFilter(admin.SimpleListFilter):
    title = _("巡检状态")
    parameter_name = "attention"

    def lookups(self, request, model_admin):
        return (
            ("normal", _("正常")),
            ("stalled", _("超时未投递")),
        )

    def queryset(self, request, queryset):
        if self.value() == "normal":
            return queryset.exclude(
                status=WebhookEvent.Status.PENDING,
                created_at__lte=timezone.now()
                - OperationalRiskService.webhook_event_timeout(),
            )
        if self.value() == "stalled":
            return queryset.filter(
                status=WebhookEvent.Status.PENDING,
                created_at__lte=timezone.now()
                - OperationalRiskService.webhook_event_timeout(),
            )
        return queryset


class DeliveryAttemptInline(TabularInline):
    """事件详情页直接列出历次投递，排障时不必再跳到投递日志反查。"""

    model = DeliveryAttempt
    extra = 0
    can_delete = False
    tab = True
    show_count = True
    # 每行的表头是模型 __str__，在已经逐列展开的只读表里纯属噪音。
    show_title = False
    per_page = 10
    ordering = ("-try_number",)
    verbose_name = _("投递尝试")
    verbose_name_plural = _("投递尝试")
    fields = (
        "try_number",
        "ok",
        "display_response_status",
        "duration_ms",
        "error",
        "created_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @display(description=_("响应状态码"))
    def display_response_status(self, obj: DeliveryAttempt):
        # response_status 模型字段没有 verbose_name，直接入列会渲染成英文列头。
        return obj.response_status if obj.response_status is not None else fmt.empty()


@admin.register(WebhookEvent)
class WebhookEventAdmin(ReadOnlyModelAdmin):
    inlines = (DeliveryAttemptInline,)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("project",)
    list_filter_sheet = False
    list_display = (
        "display_identity",
        "project",
        "display_status",
        "display_attempt_count",
        "display_attention",
        "schedule_locked_until",
        "created_at",
    )
    readonly_fields = (
        "project",
        "nonce",
        "display_payload",
        "status",
        "display_attempt_count",
        "display_last_error",
        "delivered_at",
        "delivery_url",
        "delivery_method",
        "expected_response_body",
        "schedule_locked_until",
        "delivery_locked_until",
        "created_at",
    )

    fieldsets = (
        (
            _("事件"),
            {
                "classes": ("tab",),
                "fields": (
                    "nonce",
                    "project",
                    "status",
                    "display_attempt_count",
                    "delivered_at",
                    "created_at",
                ),
            },
        ),
        (
            _("投递配置"),
            {
                "classes": ("tab",),
                "fields": (
                    "delivery_url",
                    "delivery_method",
                    "expected_response_body",
                ),
            },
        ),
        (
            _("重试与锁"),
            {
                "classes": ("tab",),
                "fields": (
                    "display_last_error",
                    "schedule_locked_until",
                    "delivery_locked_until",
                ),
            },
        ),
        (
            _("载荷"),
            {
                "classes": ("tab",),
                "fields": ("display_payload",),
            },
        ),
    )

    actions = ["mark_as_pending"]
    list_filter = (
        ("status", ChoicesDropdownFilter),
        EventAttentionFilter,
        ("project", RelatedDropdownFilter),
        ("created_at", RangeDateTimeFilter),
    )
    search_fields = ("nonce", "project__name")
    search_help_text = _("支持按事件 nonce 或项目名称搜索")

    @display(description=_("事件"), ordering="nonce", header=True)
    def display_identity(self, instance: WebhookEvent):
        payload_type = ""
        if isinstance(instance.payload, dict):
            payload_type = str(instance.payload.get("type") or "")
        return (instance.nonce, payload_type)

    @display(
        description=_("状态"),
        ordering="status",
        label={
            WebhookEvent.Status.PENDING: "warning",
            WebhookEvent.Status.SUCCEEDED: "success",
            WebhookEvent.Status.FAILED: "danger",
        },
    )
    def display_status(self, instance: WebhookEvent):
        return (instance.status, instance.get_status_display())

    @display(description=_("尝试次数"), ordering="attempt_count")
    def display_attempt_count(self, instance: WebhookEvent):
        # 直接读模型字段：它在认领投递时原子自增，比 attempts 关联表计数更准
        # （任务在写 attempt 前被杀时，关联表不会增长）。
        return instance.attempt_count

    @display(
        description=_("巡检"),
        label={
            "normal": "success",
            "stalled": "danger",
        },
    )
    def display_attention(self, instance: WebhookEvent):
        if (
            instance.status == WebhookEvent.Status.PENDING
            and instance.created_at
            <= timezone.now() - OperationalRiskService.webhook_event_timeout()
        ):
            return ("stalled", _("超时"))
        return ("normal", _("正常"))

    @display(description=_("内容"))
    def display_payload(self, instance: WebhookEvent):
        return fmt.scroll_box(instance.payload)

    @display(description=_("投递报错信息"))
    def display_last_error(self, instance: WebhookEvent):
        return fmt.scroll_box(instance.last_error)

    @admin.action(description=_("重新投递"))
    def mark_as_pending(self, request, queryset):
        queryset = queryset.filter(status=WebhookEvent.Status.FAILED)
        # 重新投递只影响选中的事件。
        # 清除调度/投递锁，避免事件在退避窗口或旧 worker claim 内仍被跳过。
        # attempt_count 必须归零：失败事件的计数已经到达上限，不重置的话第一次
        # 投递不成功就会立刻再次终局，人工重投等于只换来一次机会。
        queryset.update(
            status=WebhookEvent.Status.PENDING,
            attempt_count=0,
            schedule_locked_until=None,
            delivery_locked_until=None,
        )
        self.message_user(request, _("已进入待投递队列"))


@admin.register(DeliveryAttempt)
class DeliveryAttemptAdmin(ReadOnlyModelAdmin):
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("event", "event__project")
    list_filter_sheet = False
    list_display = (
        "display_identity",
        "display_project",
        "display_result",
        "display_response_status",
        "display_duration",
        "created_at",
    )
    search_fields = ("event__nonce", "event__project__name")
    search_help_text = _("支持按事件 nonce 或项目名称搜索")
    list_filter = (
        "ok",
        ("event__project", RelatedDropdownFilter),
        ("created_at", RangeDateTimeFilter),
    )
    readonly_fields = (
        "display_response_status",
        "display_request_headers",
        "display_request_body",
        "display_response_body",
    )
    fieldsets = (
        (
            _("投递"),
            {
                "classes": ("tab",),
                "fields": (
                    "event",
                    "try_number",
                    "ok",
                    "duration_ms",
                    "error",
                    "created_at",
                ),
            },
        ),
        (
            _("请求"),
            {
                "classes": ("tab",),
                "fields": (
                    "display_request_headers",
                    "display_request_body",
                ),
            },
        ),
        (
            _("响应"),
            {
                "classes": ("tab",),
                "fields": (
                    "display_response_status",
                    "display_response_body",
                ),
            },
        ),
    )
    exclude = ("response_headers", "request_headers", "request_body", "response_body")

    @display(description=_("事件"), ordering="try_number", header=True)
    def display_identity(self, instance: DeliveryAttempt):
        return (
            instance.event.nonce,
            _("第 %(n)s 次尝试") % {"n": instance.try_number},
        )

    @display(description=_("项目"), ordering="event__project__name")
    def display_project(self, instance: DeliveryAttempt):
        return instance.event.project

    @display(
        description=_("结果"),
        ordering="ok",
        label={"ok": "success", "failed": "danger"},
    )
    def display_result(self, instance: DeliveryAttempt):
        return ("ok", _("成功")) if instance.ok else ("failed", _("失败"))

    @display(description=_("耗时"), ordering="duration_ms")
    def display_duration(self, instance: DeliveryAttempt):
        if instance.duration_ms is None:
            return fmt.empty()
        return fmt.number(instance.duration_ms, unit="ms")

    @display(description=_("响应状态码"), ordering="response_status")
    def display_response_status(self, instance: DeliveryAttempt):
        return (
            instance.response_status
            if instance.response_status is not None
            else fmt.empty()
        )

    @display(description=_("请求头"))
    def display_request_headers(self, instance: DeliveryAttempt):
        return fmt.scroll_box(instance.request_headers)

    @display(description=_("请求体"))
    def display_request_body(self, instance: DeliveryAttempt):
        return fmt.scroll_box(instance.request_body)

    @display(description=_("响应体"))
    def display_response_body(self, instance: DeliveryAttempt):
        return fmt.scroll_box(instance.response_body)
