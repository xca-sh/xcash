from django.contrib import admin
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import path
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST
from unfold.admin import StackedInline
from unfold.decorators import display

from alerts.models import ProjectAlertState
from alerts.models import ProjectTelegramAlertConfig
from common.admin import ModelAdmin
from common.admin import ReadOnlyModelAdmin


class TelegramTestDispatchError(Exception):
    pass


class ProjectTelegramAlertConfigInline(StackedInline):
    model = ProjectTelegramAlertConfig
    extra = 0
    max_num = 1
    can_delete = False
    verbose_name = _("Telegram 告警")
    verbose_name_plural = _("Telegram 告警")
    fields = (
        "enabled",
        "telegram_chat_id",
        "telegram_thread_id",
        "notify_on_withdrawal_stalled",
        "notify_on_deposit_collection_stalled",
        "notify_on_contract_collection_stalled",
        "notify_on_webhook_stalled",
        "notify_on_recovery",
        "display_send_test_action",
        "last_verified_at",
        "last_test_sent_at",
        "last_error_at",
        "last_error_message",
    )
    readonly_fields = (
        "display_send_test_action",
        "last_verified_at",
        "last_test_sent_at",
        "last_error_at",
        "last_error_message",
    )

    @display(description=_("测试消息"))
    def display_send_test_action(self, instance: ProjectTelegramAlertConfig):
        if not instance.pk:
            return _("请先保存项目后再发送测试消息")
        send_test_url = reverse(
            "admin:alerts_projecttelegramalertconfig_send_test",
            args=[instance.pk],
        )
        # Inline 里直接提供测试按钮，避免负责人再跳转到单独的告警列表页。
        # 使用 POST form 而非 <a> 链接，防止浏览器预取等意外触发。
        # CSRF token 从当前页面已有的表单中复用（admin change form 必定包含）。
        return format_html(
            '<form method="post" action="{}" style="display:inline" '
            "onsubmit=\"this.querySelector('[name=csrfmiddlewaretoken]').value="
            "document.querySelector('[name=csrfmiddlewaretoken]').value;return true\">"
            '<input type="hidden" name="csrfmiddlewaretoken" value="">'
            '<button type="submit" class="font-medium inline-flex items-center gap-2 rounded-default '
            "border border-base-200 bg-primary-600 px-5 py-2.5 text-[14px] text-white "
            'hover:bg-primary-600/80">{}</button></form>',
            send_test_url,
            _("发送 Telegram 测试消息"),
        )


@admin.register(ProjectTelegramAlertConfig)
class ProjectTelegramAlertConfigAdmin(ModelAdmin):
    list_display = (
        "project",
        "enabled",
        "display_target",
        "display_subscription_summary",
        "last_verified_at",
        "last_test_sent_at",
        "last_error_at",
        "updated_at",
    )
    list_filter = ("enabled", "notify_on_recovery")
    search_fields = ("project__name", "telegram_chat_id")
    actions = ("send_test_message",)

    fieldsets = (
        (
            _("Telegram"),
            {
                "fields": (
                    "project",
                    "enabled",
                    "telegram_chat_id",
                    "telegram_thread_id",
                ),
            },
        ),
        (
            _("告警范围"),
            {
                "fields": (
                    "notify_on_withdrawal_stalled",
                    "notify_on_deposit_collection_stalled",
                    "notify_on_contract_collection_stalled",
                    "notify_on_webhook_stalled",
                    "notify_on_recovery",
                ),
            },
        ),
        (
            _("状态"),
            {
                "fields": (
                    "last_verified_at",
                    "last_test_sent_at",
                    "last_error_at",
                    "last_error_message",
                ),
            },
        ),
    )
    readonly_fields = (
        "last_verified_at",
        "last_test_sent_at",
        "last_error_at",
        "last_error_message",
    )

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<path:object_id>/send-test/",
                self.admin_site.admin_view(require_POST(self.send_test_view)),
                name="alerts_projecttelegramalertconfig_send_test",
            ),
        ]
        return custom_urls + urls

    def save_model(self, request, obj, form, change):
        if change:
            obj.updated_by = request.user
        else:
            obj.created_by = request.user
            obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    @display(description=_("目标"))
    def display_target(self, instance: ProjectTelegramAlertConfig):
        return instance.target_label

    @display(description=_("订阅范围"))
    def display_subscription_summary(self, instance: ProjectTelegramAlertConfig):
        parts = []
        if instance.notify_on_withdrawal_stalled:
            parts.append(str(_("提币")))
        if instance.notify_on_deposit_collection_stalled:
            parts.append(str(_("归集")))
        if instance.notify_on_contract_collection_stalled:
            parts.append(str(_("合约归集")))
        if instance.notify_on_webhook_stalled:
            parts.append(str(_("Webhook")))
        if instance.notify_on_recovery:
            parts.append(str(_("恢复")))
        return " / ".join(parts) or "-"

    @admin.action(description=_("发送 Telegram 测试消息"))
    def send_test_message(self, request, queryset):
        sent_count = 0
        for config in queryset:
            try:
                self._queue_test_message(request=request, config=config)
            except TelegramTestDispatchError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
                return
            sent_count += 1

        self.message_user(
            request,
            _("已为 %(count)s 个项目排队发送测试消息") % {"count": sent_count},
            level=messages.SUCCESS,
        )

    def send_test_view(self, request, object_id):
        config = self.get_object(request, object_id)
        if config is None:
            self.message_user(request, _("告警配置不存在"), level=messages.ERROR)
            return redirect("admin:index")
        try:
            self._queue_test_message(request=request, config=config)
        except TelegramTestDispatchError as exc:
            self.message_user(request, str(exc), level=messages.ERROR)
            return redirect(
                reverse("admin:projects_project_change", args=[config.project_id])
            )
        self.message_user(
            request,
            _("已为项目 %(project)s 排队发送测试消息")
            % {"project": config.project.name},
            level=messages.SUCCESS,
        )
        return redirect(
            reverse("admin:projects_project_change", args=[config.project_id])
        )

    def _queue_test_message(
        self, *, request, config: ProjectTelegramAlertConfig
    ) -> None:
        from django.conf import settings

        from alerts.tasks import send_project_telegram_test

        # 测试发送不是资金动作，只保留任务队列异步隔离，不再要求 fresh OTP。
        if not settings.ALERTS_TELEGRAM_BOT_TOKEN.strip():
            raise TelegramTestDispatchError(
                "ALERTS_TELEGRAM_BOT_TOKEN 未配置，无法发送测试消息"
            )
        send_project_telegram_test.delay(config_id=config.pk)


@admin.register(ProjectAlertState)
class ProjectAlertStateAdmin(ReadOnlyModelAdmin):
    list_display = (
        "title",
        "project",
        "event_type",
        "severity",
        "status",
        "object_type",
        "object_pk",
        "last_seen_at",
        "last_sent_at",
        "resolved_at",
    )
    list_filter = ("event_type", "severity", "status")
    search_fields = ("title", "project__name", "detail", "fingerprint")
