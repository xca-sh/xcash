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
    # 归集延迟按链类型分开：EVM 默认 60 分钟，在到账时效和批量归集之间折中；
    # Tron 归集一次能量/带宽成本高，长延迟把同槽位多笔到账批量摊薄成本。
    # 两者都只影响「确认后等多久再聚合归集」，不影响是否归集。
    evm_vault_slot_collect_delay_minutes = models.PositiveIntegerField(
        _("EVM VaultSlot 归集延迟(分钟)"),
        default=60,
        validators=[MinValueValidator(0)],
        help_text=_(
            "EVM 到账确认后等待该时间再聚合归集，期间同槽位同币种不重复创建归集计划。"
            "EVM 默认 60 分钟，兼顾到账归集时效和同槽位批量归集。"
        ),
    )
    tron_vault_slot_collect_delay_minutes = models.PositiveIntegerField(
        _("Tron VaultSlot 归集延迟(分钟)"),
        default=360,
        validators=[MinValueValidator(0)],
        help_text=_(
            "Tron 到账确认后等待该时间再聚合归集，期间同槽位同币种不重复创建归集计划。"
            "Tron 归集成本高，默认长延迟批量摊薄。"
        ),
    )
    vault_slot_collect_min_worth_usd = models.DecimalField(
        _("VaultSlot 最小归集价值(USD)"),
        max_digits=16,
        decimal_places=6,
        default=Decimal("1"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text=_(
            "归集计划到期执行时，槽位该币种余额价值低于该阈值则放弃本次归集"
            "（粉尘归集 gas 必亏；后续入账使总额达标后会再次调度）。0 表示不限制。"
        ),
    )
    invoice_vault_slot_limit_per_project_chain = models.PositiveIntegerField(
        _("账单 VaultSlot 单项目单链上限"),
        default=100,
        validators=[MinValueValidator(1)],
        help_text=_(
            "单个项目在单条链上最多可创建的账单收款 VaultSlot 数量。"
            "达到上限后仍可复用已有可用槽位，但不再创建新槽位。"
        ),
    )
    aml_screening_enabled = models.BooleanField(
        _("开启 AML 筛查"),
        default=False,
        help_text=_("开启后对高于 AML 查询阈值的账单收款和充值收款查询外部地址风险。"),
    )
    aml_screening_threshold_usd = models.DecimalField(
        _("AML 查询阈值(USD)"),
        max_digits=16,
        decimal_places=6,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text=_("账单收款或充值收款价值大于该阈值时执行 AML 查询。"),
    )
    aml_screening_cache_seconds = models.PositiveIntegerField(
        _("AML 查询缓存秒数"),
        default=3600,
        validators=[MinValueValidator(1)],
        help_text=_("同一地址 AML 查询成功后在 Django 缓存中保留的秒数。"),
    )
    aml_screening_force_refresh_threshold_usd = models.DecimalField(
        _("AML 查询强制刷新阈值(USD)"),
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
        return str(_("系统运行参数"))

    def save(self, *args, **kwargs):
        # 强制收口为单例记录，避免后台误建第二份配置导致读取口径分叉。
        self.singleton_key = 1
        super().save(*args, **kwargs)
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)

    def delete(self, *args, **kwargs):
        raise RuntimeError("系统运行参数不允许删除")


class SystemWallet(models.Model):
    """全平台唯一系统热钱包归属声明。

    Wallet 负责助记词托管与地址派生；
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
        verbose_name=_("系统热钱包"),
        help_text=_("用于平台基础设施交易，例如统一部署 VaultSlot 合约。"),
    )
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)

    class Meta:
        verbose_name = _("系统热钱包")
        verbose_name_plural = verbose_name

    def __str__(self):
        return str(_("系统热钱包"))  # noqa

    def save(self, *args, **kwargs):
        # 强制收口为单例记录，避免系统热钱包入口分叉。
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
        raise RuntimeError("系统热钱包不允许删除")
