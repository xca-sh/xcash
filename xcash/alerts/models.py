from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class ProjectAlertEventType(models.TextChoices):
    WITHDRAWAL_STALLED = "withdrawal_stalled", _("提币卡单")
    DEPOSIT_COLLECTION_STALLED = "deposit_collection_stalled", _("归集卡单")
    CONTRACT_COLLECTION_STALLED = "contract_collection_stalled", _("合约归集卡单")
    WEBHOOK_STALLED = "webhook_stalled", _("Webhook 堆积")


class ProjectAlertSeverity(models.TextChoices):
    # 告警级别使用显式语义值，避免和排期优先级 P0/P1/P2 混淆。
    CRITICAL = "critical", _("严重")
    HIGH = "high", _("高")
    MEDIUM = "medium", _("中")


class ProjectAlertStatus(models.TextChoices):
    OPEN = "open", _("处理中")
    RESOLVED = "resolved", _("已恢复")


class ProjectTelegramAlertConfig(models.Model):
    """项目级 Telegram 告警配置。

    第一性原则：项目负责人真正需要维护的是“这个项目的 Telegram 往哪里发”，
    不是一个通用多通道通知平台。因此这里直接建模成 Project -> Telegram 配置。
    """

    project = models.OneToOneField(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="telegram_alert_config",
        verbose_name=_("项目"),
    )
    enabled = models.BooleanField(_("启用"), default=True)
    telegram_chat_id = models.CharField(_("Telegram Chat ID"), max_length=64)
    telegram_thread_id = models.CharField(
        _("Telegram Thread ID"),
        max_length=64,
        blank=True,
        default="",
    )
    notify_on_withdrawal_stalled = models.BooleanField(_("提币卡单告警"), default=True)
    notify_on_deposit_collection_stalled = models.BooleanField(
        _("归集卡单告警"),
        default=True,
    )
    notify_on_contract_collection_stalled = models.BooleanField(
        _("合约归集卡单告警"),
        default=True,
    )
    notify_on_webhook_stalled = models.BooleanField(_("Webhook 堆积告警"), default=True)
    notify_on_recovery = models.BooleanField(_("恢复通知"), default=True)
    last_verified_at = models.DateTimeField(_("最近验证时间"), null=True, blank=True)
    last_test_sent_at = models.DateTimeField(
        _("最近测试发送时间"), null=True, blank=True
    )
    last_error_message = models.TextField(_("最近错误"), blank=True, default="")
    last_error_at = models.DateTimeField(_("最近报错时间"), null=True, blank=True)
    created_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_project_telegram_alert_configs",
        verbose_name=_("创建人"),
    )
    updated_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_project_telegram_alert_configs",
        verbose_name=_("更新人"),
    )
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)

    class Meta:
        verbose_name = _("项目 Telegram 告警配置")
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return self.project.name

    def clean(self) -> None:
        super().clean()
        if not self.telegram_chat_id:
            raise ValidationError({"telegram_chat_id": _("Telegram Chat ID 不能为空")})
        if self.telegram_thread_id:
            try:
                int(self.telegram_thread_id)
            except (ValueError, TypeError) as exc:
                raise ValidationError(
                    {"telegram_thread_id": _("Telegram Thread ID 必须为数字")}
                ) from exc

    def supports_event(self, event_type: str) -> bool:
        event_flags = {
            ProjectAlertEventType.WITHDRAWAL_STALLED: self.notify_on_withdrawal_stalled,
            ProjectAlertEventType.DEPOSIT_COLLECTION_STALLED: self.notify_on_deposit_collection_stalled,
            ProjectAlertEventType.CONTRACT_COLLECTION_STALLED: self.notify_on_contract_collection_stalled,
            ProjectAlertEventType.WEBHOOK_STALLED: self.notify_on_webhook_stalled,
        }
        return bool(event_flags.get(event_type, False))

    @property
    def target_label(self) -> str:
        thread_suffix = f"/{self.telegram_thread_id}" if self.telegram_thread_id else ""
        return f"{self.telegram_chat_id}{thread_suffix}"


class ProjectAlertState(models.Model):
    """项目级告警状态。

    告警系统的最小必要状态只有两件事：
    1. 某个异常对象当前是否仍在告警
    2. 上次什么时候发过，避免重复轰炸
    """

    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="alert_states",
        verbose_name=_("项目"),
    )
    event_type = models.CharField(
        _("事件类型"), max_length=64, choices=ProjectAlertEventType
    )
    object_type = models.CharField(_("对象类型"), max_length=32)
    object_pk = models.PositiveBigIntegerField(_("对象 ID"))
    fingerprint = models.CharField(_("指纹"), max_length=128, unique=True)
    severity = models.CharField(_("级别"), max_length=16, choices=ProjectAlertSeverity)
    title = models.CharField(_("标题"), max_length=128)
    detail = models.TextField(_("详情"))
    admin_url = models.CharField(_("后台链接"), max_length=255, blank=True, default="")
    status = models.CharField(
        _("状态"),
        max_length=16,
        choices=ProjectAlertStatus,
        default=ProjectAlertStatus.OPEN,
    )
    first_seen_at = models.DateTimeField(_("首次发现时间"))
    last_seen_at = models.DateTimeField(_("最近发现时间"))
    last_sent_at = models.DateTimeField(_("最近发送时间"), null=True, blank=True)
    resolved_at = models.DateTimeField(_("恢复时间"), null=True, blank=True)
    notify_count = models.PositiveIntegerField(_("通知次数"), default=0)
    last_error_message = models.TextField(_("最近发送错误"), blank=True, default="")
    last_error_at = models.DateTimeField(_("最近发送报错时间"), null=True, blank=True)
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)

    class Meta:
        # 兼容已执行过旧版初始迁移的数据库，继续复用原状态表名。
        db_table = "alerts_projecttelegramalertstate"
        constraints = [
            models.UniqueConstraint(
                fields=("project", "event_type", "object_type", "object_pk"),
                name="uniq_project_alert_state_object",
            ),
        ]
        ordering = ("-last_seen_at",)
        verbose_name = _("项目告警状态")
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.project.name}:{self.event_type}:{self.object_pk}"

    def mark_resolved(self, *, resolved_at=None) -> None:
        # 状态模型表达的是告警生命周期，而不是 Telegram 渠道本身。
        self.status = ProjectAlertStatus.RESOLVED
        self.resolved_at = resolved_at or timezone.now()
