"""core app 仅保留后台看板、系统初始化入口与系统级运行对象。"""

from decimal import Decimal

from django.core.cache import cache
from django.core.validators import MinValueValidator
from django.db import IntegrityError
from django.db import models
from django.db import transaction
from django.utils.translation import gettext_lazy as _

SYSTEM_SETTINGS_CACHE_KEY = "core:system_settings:singleton"


class SystemSettings(models.Model):
    """系统级运行参数中心。

    第一性原则：只有“上线后可能调整、且不属于密钥/部署机密”的运行参数，
    才应该进入后台由超管维护。
    """

    singleton_key = models.PositiveSmallIntegerField(
        _("单例键"),
        default=1,
        unique=True,
        editable=False,
    )
    admin_session_timeout_minutes = models.PositiveIntegerField(
        _("后台会话超时(分钟)"),
        default=10,
        validators=[MinValueValidator(1)],
        help_text=_("管理员登录后台后，超过该时间无操作需重新登录。"),
    )
    admin_sensitive_action_otp_max_age_seconds = models.PositiveIntegerField(
        _("敏感动作两步验证超时(秒)"),
        default=900,
        validators=[MinValueValidator(60)],
        help_text=_(
            "超过该时间后，项目配置等高风险动作需要重新验证两步验证码。"
        ),
    )
    alerts_repeat_interval_minutes = models.PositiveIntegerField(
        _("告警重复发送间隔(分钟)"),
        default=30,
        validators=[MinValueValidator(1)],
        help_text=_("同一项目同一告警在该时间窗内不重复发送。"),
    )
    webhook_delivery_breaker_threshold = models.PositiveIntegerField(
        _("Webhook 熔断阈值"),
        default=30,
        validators=[MinValueValidator(1)],
        help_text=_("连续失败达到该次数后，自动关闭项目 Webhook 投递。"),
    )
    webhook_delivery_max_retries = models.PositiveIntegerField(
        _("Webhook 最大重试次数"),
        default=5,
        validators=[MinValueValidator(1)],
        help_text=_("单个 Webhook 事件最多自动重试的次数。"),
    )
    webhook_delivery_max_backoff_seconds = models.PositiveIntegerField(
        _("Webhook 最大退避秒数"),
        default=120,
        validators=[MinValueValidator(1)],
        help_text=_("Webhook 自动重试的指数退避上限秒数。"),
    )
    webhook_event_timeout_minutes = models.PositiveIntegerField(
        _("Webhook 堆积超时(分钟)"),
        default=15,
        validators=[MinValueValidator(1)],
        help_text=_("待投递 Webhook 事件超过该时间仍未送达时，进入异常巡检。"),
    )
    vault_slot_collect_delay_minutes = models.PositiveIntegerField(
        _("VaultSlot 归集延迟(分钟)"),
        default=360,
        validators=[MinValueValidator(0)],
        help_text=_(
            "ERC20 到账确认后等待该时间再聚合归集，期间同槽位同币种不重复创建归集计划。"
        ),
    )
    risk_marking_enabled = models.BooleanField(
        _("开启风险标记"),
        default=False,
        help_text=_("开启后对高于风险查询阈值的账单和充币查询外部地址风险。"),
    )
    risk_marking_threshold_usd = models.DecimalField(
        _("风险查询阈值(USD)"),
        max_digits=16,
        decimal_places=6,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text=_("账单或充币价值大于该阈值时查询风险。"),
    )
    risk_marking_cache_seconds = models.PositiveIntegerField(
        _("风险查询缓存秒数"),
        default=3600,
        validators=[MinValueValidator(1)],
        help_text=_("同一地址风险查询成功后在 Django 缓存中保留的秒数。"),
    )
    risk_marking_force_refresh_threshold_usd = models.DecimalField(
        _("风险查询强制刷新阈值(USD)"),
        max_digits=16,
        decimal_places=6,
        default=Decimal("10000"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text=_("本次业务价值大于该阈值时跳过缓存直接查询。"),
    )
    quicknode_misttrack_endpoint_url = models.URLField(
        _("QuickNode MistTrack Endpoint"),
        blank=True,
        default="",
        help_text=_("QuickNode MistTrack add-on 的 JSON-RPC endpoint URL。"),
    )
    misttrack_openapi_api_key = models.CharField(
        _("MistTrack OpenAPI API Key"),
        max_length=255,
        blank=True,
        default="",
        help_text=_(
            "MistTrack 官方 OpenAPI API Key；配置后优先使用 MistTrack V3 风险评分接口。"
        ),
    )
    created_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_system_settings",
        verbose_name=_("创建人"),
    )
    updated_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_system_settings",
        verbose_name=_("更新人"),
    )
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)

    class Meta:
        verbose_name = _("系统运行参数")
        verbose_name_plural = verbose_name

    def __str__(self):
        return _("系统运行参数")

    def save(self, *args, **kwargs):
        # 强制收口为单例记录，避免后台误建第二份配置导致读取口径分叉。
        self.singleton_key = 1
        super().save(*args, **kwargs)
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)

    def delete(self, *args, **kwargs):
        raise RuntimeError("系统运行参数不允许删除")


class SystemWallet(models.Model):
    """全平台唯一系统级热钱包归属声明。

    Wallet 负责助记词托管与地址派生；SystemWallet 只表达
    “系统级热钱包业务归属”，避免和项目热钱包混淆。
    """

    singleton_key = models.PositiveSmallIntegerField(
        _("单例键"),
        default=1,
        unique=True,
        editable=False,
    )
    wallet = models.OneToOneField(
        "chains.Wallet",
        on_delete=models.PROTECT,
        related_name="system_wallet",
        verbose_name=_("系统级热钱包"),
        help_text=_("用于平台基础设施交易，例如统一部署 VaultSlot 合约。"),
    )
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)

    class Meta:
        verbose_name = _("系统级热钱包")
        verbose_name_plural = verbose_name

    def __str__(self):
        return str(_("系统级热钱包"))

    def save(self, *args, **kwargs):
        # 强制收口为单例记录，避免系统级热钱包入口分叉。
        self.singleton_key = 1
        super().save(*args, **kwargs)

    @classmethod
    def get_current(cls) -> "SystemWallet":
        system_wallet = cls.objects.select_related("wallet").order_by("pk").first()
        if system_wallet is not None:
            return system_wallet

        from chains.models import Wallet

        try:
            with transaction.atomic():
                # Wallet.generate() 在主系统内部生成并加密助记词，密钥材料不出系统。
                wallet = Wallet.generate()
                return cls.objects.create(wallet=wallet)
        except IntegrityError:
            return cls.objects.select_related("wallet").get(singleton_key=1)

    def delete(self, *args, **kwargs):
        raise RuntimeError("系统级热钱包不允许删除")
