from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from shortuuid.django_fields import ShortUUIDField

from chains.models import Chain
from common.consts import UPPER_ALPHABET
from common.fields import AddressField


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
        help_text=_("用于本网关发送通知到商户后端"),
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
        help_text=_("低于该金额的账单无需等待区块确认数，立即确认"),
    )
    hmac_key = ShortUUIDField(
        verbose_name=_("HMAC密钥"),
        length=32,
    )
    vault = AddressField(
        _("收款归集地址"),
        null=True,
        blank=True,
        help_text=_(
            "用于生成 EVM VaultSlot 合约的不可变 vault。留空时禁止生成 VaultSlot；"
            "一旦设置不可修改。"
        ),
        unique=True,
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
                raise ValidationError({"vault": _("收款归集地址一旦设置不可修改。")})
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
        if not self.vault:
            errors.append(_("多签钱包地址未配置"))  # noqa
        if not self.ip_white_list:
            errors.append(_("IP 白名单未配置"))  # noqa
        if not self.webhook:
            errors.append(_("通知地址未配置"))  # noqa

        return (not errors), errors

    def recipients(self, chain: Chain):
        from invoices.models import DifferRecipientAddress

        return set(
            DifferRecipientAddress.objects.filter(
                project=self,
                chain_type=chain.type,
            ).values_list(
                "address",
                flat=True,
            ),
        )


class Customer(models.Model):
    """商户的终端客户：以 (project, uid) 在项目内唯一标识，与后台登录账号 User 无关。"""

    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        verbose_name=_("项目"),
    )
    uid = models.CharField(
        db_index=True,
        verbose_name=_("客户UID"),
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="加入时间")

    class Meta:
        # 统一采用具名 UniqueConstraint，便于数据库约束报错定位和后续约束扩展。
        constraints = [
            models.UniqueConstraint(
                fields=("uid", "project"),
                name="uniq_customer_uid_project",
            ),
        ]
        verbose_name = _("客户")
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.uid
