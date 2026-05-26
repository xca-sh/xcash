from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from shortuuid.django_fields import ShortUUIDField

from chains.capabilities import ChainProductCapabilityService
from chains.models import Chain
from chains.models import ChainType
from common.consts import UPPER_ALPHABET
from common.fields import AddressField
from common.fields import EvmAddressField


class Project(models.Model):
    appid = ShortUUIDField(
        verbose_name=_("Appid"),
        prefix="XC-",
        alphabet=UPPER_ALPHABET,
        db_index=True,
        editable=False,
        unique=True,
        length=8,
    )
    name = models.CharField(
        verbose_name=_("项目名称"),
        help_text=_("对外作为商户名展示"),
        unique=True,
    )
    wallet = models.OneToOneField(
        "chains.Wallet",
        on_delete=models.CASCADE,
        verbose_name=_("项目级热钱包"),
        help_text=_("用于项目提币、归集等项目资产流转交易。"),
    )
    ip_white_list = models.TextField(
        _("IP白名单"),
        default="*",
        help_text=mark_safe(  # noqa: S308 — admin help_text，内容为硬编码中文字符串，无 XSS 风险
            _("只有符合白名单的 IP 才可以与本网关交互，支持 IP 地址或 IP 网段")
            + "<br>"
            + _("可同时设置多个，中间用英文逗号 ',' 分割")
            + "<br>"
            + _("* 代表允许所有 IP 访问")
        ),
    )
    webhook = models.URLField(
        _("通知地址"),
        blank=True,
        default="",
        help_text=_("用于本网关发送通知到项目后端"),
    )
    webhook_open = models.BooleanField(verbose_name=_("通知状态"), default=True)
    failed_count = models.PositiveIntegerField(
        default=0,
        verbose_name=_("连续失败次数"),
    )
    pre_notify = models.BooleanField(
        _("开启预通知"),
        default=False,
        help_text="刚出块(尚未达到区块确认数)，就发送一次预通知",
    )
    fast_confirm_threshold = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("10"),
        verbose_name=_("快速确认阈值（USD）"),
        help_text=_("低于该金额的账单无需等待，立即确认"),
    )
    hmac_key = ShortUUIDField(
        verbose_name=_("HMAC密钥"),
        length=32,
    )
    vault = AddressField(
        _("DepositSlot 多签归集地址"),
        null=True,
        blank=True,
        help_text=_(
            "用于生成 EVM DepositSlot 合约的不可变 vault。留空时禁止生成 DepositSlot；"
            "一旦设置不可修改。"
        ),
        unique=True,
    )

    withdrawal_review_required = models.BooleanField(
        _("提币需审核"),
        default=True,
        help_text=_(
            "开启后，新提币请求会先进入审核中，需后台批准后才会进入链上发送队列"
        ),
    )
    withdrawal_review_exempt_limit = models.DecimalField(
        _("免审核门槛(USD)"),
        max_digits=16,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_(
            "仅在开启提币审核时生效；低于该金额的提币可直接进入链上发送队列，留空表示全部需要审核"
        ),
    )
    withdrawal_single_limit = models.DecimalField(
        _("单笔提币限额(USD)"),
        max_digits=16,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("留空表示不限额；超出时直接拒绝创建提币请求"),
    )
    withdrawal_daily_limit = models.DecimalField(
        _("单日提币限额(USD)"),
        max_digits=16,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("留空表示不限额；当天已创建的提币请求也会占用额度"),
    )

    active = models.BooleanField(verbose_name=_("启用"), default=True)
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("创建时间"))

    class Meta:
        verbose_name = _("项目")
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.pk is not None:
            old_vault = (
                self.__class__.objects.filter(pk=self.pk)
                .values_list("vault", flat=True)
                .first()
            )
            if old_vault and self.vault != old_vault:
                raise ValidationError(
                    {"vault": _("DepositSlot 多签归集地址一旦设置不可修改。")}
                )
        return super().save(*args, **kwargs)

    @classmethod
    def retrieve(cls, appid: str):
        try:
            return cls.objects.get(appid=appid)
        except cls.DoesNotExist:
            return None

    @property
    def is_ready(self) -> tuple[bool, list[str]]:
        # 错误项采用统一的"短名词 + 状态"格式，便于前端横排拼接（如"通知地址未配置、差额账单收款地址未配置"）
        errors: list[str] = []
        if not self.ip_white_list:
            errors.append(_("IP 白名单未配置"))
        if not self.webhook:
            errors.append(_("通知地址未配置"))
        if not DifferRecipientAddress.objects.filter(project=self).exists():
            errors.append(_("差额账单收款地址未配置"))
        return (not errors), errors

    def recipients(self, chain: Chain):
        return set(
            DifferRecipientAddress.objects.filter(
                project=self,
                chain_type=chain.type,
            ).values_list(
                "address",
                flat=True,
            ),
        )


def status(request):
    return ""


class DifferRecipientAddress(models.Model):
    """差额账单的商户收款地址。

    新架构下合约账单不再使用该模型分配收款地址；它只服务于差额账单，
    用于在没有 DepositSlot 合约收款方案的链上扫描买家入账。
    """

    name = models.CharField(verbose_name=_("备注名称"), blank=True)
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        verbose_name=_("项目"),
    )
    chain_type = models.CharField(
        _("地址格式"),
        choices=ChainType,
        help_text="EVM: Ethereum, BSC, Polygon, Base...<br>Tron: Tron",
    )
    address = EvmAddressField(verbose_name=_("差额账单收款地址"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("创建时间"))

    class Meta:
        # 统一采用具名 UniqueConstraint，便于数据库约束报错定位和后续约束扩展。
        constraints = [
            models.UniqueConstraint(
                fields=("chain_type", "address"),
                name="uniq_differ_recipient_address_chain_type_address",
            ),
        ]
        verbose_name = _("差额账单收款地址")
        verbose_name_plural = _("差额账单收款地址")

    def __str__(self):
        return self.address

    def save(self, *args, **kwargs):
        # 差额账单收款地址的链上发现完全由内部扫描器负责；模型层只保留数据校验，不再派发外部订阅同步。
        self.full_clean()
        return super().save(*args, **kwargs)

    def clean(self) -> None:
        """校验差额账单收款地址允许进入的链类型。"""
        super().clean()
        if not self.chain_type:
            return

        if (
            self.chain_type
            not in ChainProductCapabilityService.INVOICE_RECIPIENT_CHAIN_TYPES
        ):
            raise ValidationError(
                {"chain_type": _("当前版本差额账单收款地址仅支持 EVM / Tron。")}
            )
