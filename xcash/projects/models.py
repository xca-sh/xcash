from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from shortuuid.django_fields import ShortUUIDField
from tron.codec import TronAddressCodec
from web3 import Web3

from chains.models import ChainType
from common.consts import UPPER_ALPHABET
from common.fields import AddressField


class InvoiceReceivingMode(models.TextChoices):
    VaultSlot = "vault_slot", _("VaultSlot")
    Differ = "differ", _("差额账单收款")


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
    fast_confirm_threshold = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("10"),
        verbose_name=_("快速确认阈值（USD）"),
        help_text=_("低于该金额的账单收款无需等待区块确认数，立即确认"),
    )
    hmac_key = ShortUUIDField(
        verbose_name=_("HMAC密钥"),
        length=32,
    )
    evm_vault = AddressField(
        _("EVM 收款归集地址"),
        null=True,
        blank=True,
        help_text=_(
            "用于生成 EVM VaultSlot 合约的不可变 vault，必须是 EVM checksum 地址。"
            "留空时禁止在 EVM 生成 VaultSlot；一旦设置不可修改。"
        ),
        unique=True,
    )
    tron_vault = AddressField(
        _("Tron 收款归集地址"),
        null=True,
        blank=True,
        help_text=_(
            "用于生成 Tron VaultSlot 合约的不可变 vault，必须是 Tron Base58 地址。"
            "留空时禁止在 Tron 生成 VaultSlot；"
            "一旦设置不可修改。"
        ),
        unique=True,
    )
    # 账单收款模式按链类型分开：EVM gas 便宜，默认 VaultSlot 合约归集（解锁原生币、并发更高）；
    # Tron 归集成本高，默认差额收款（零归集成本，且能观测 EOA 收原生 TRX）。
    evm_invoice_receiving_mode = models.CharField(
        _("EVM 账单收款模式"),
        choices=InvoiceReceivingMode,
        default=InvoiceReceivingMode.VaultSlot,
        help_text=_(
            "EVM 链账单收款生成收款地址时使用 VaultSlot 合约归集还是差额收款地址。"
            "EVM gas 便宜，默认 VaultSlot。"
        ),
    )
    tron_invoice_receiving_mode = models.CharField(
        _("Tron 账单收款模式"),
        choices=InvoiceReceivingMode,
        default=InvoiceReceivingMode.Differ,
        help_text=_(
            "Tron 链账单收款生成收款地址时使用 VaultSlot 合约归集还是差额收款地址。"
            "Tron 归集成本高，默认差额收款。"
        ),
    )

    active = models.BooleanField(verbose_name=_("启用"), default=True)
    is_test = models.BooleanField(
        verbose_name=_("测试项目"),
        default=False,
        help_text=_(
            "测试项目只能使用测试网链（创建充值收款地址、账单收款仅限测试网）；"
            "非测试项目只能使用主网。用于隔离主网与测试网代币，防止混淆。"
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("创建时间"))

    class Meta:
        verbose_name = _("项目")
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.validate_vault_addresses()
        if self.pk is not None:
            old_values = (
                self.__class__.objects.filter(pk=self.pk)
                .values("evm_vault", "tron_vault")
                .first()
            )
            errors = {}
            if old_values:
                for field in ("evm_vault", "tron_vault"):
                    old_vault = old_values[field]
                    if old_vault and getattr(self, field) != old_vault:
                        errors[field] = _("收款归集地址一旦设置不可修改。")
            if errors:
                raise ValidationError(errors)
        return super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        self.validate_vault_addresses()

    def validate_vault_addresses(self) -> None:
        errors = {}
        for chain_type, field in (
            (ChainType.EVM, "evm_vault"),
            (ChainType.TRON, "tron_vault"),
        ):
            value = getattr(self, field)
            if not value:
                continue
            try:
                self.validate_vault_address_for_chain_type(
                    chain_type=chain_type,
                    address=value,
                )
            except ValidationError as exc:
                errors[field] = exc.message
        if errors:
            raise ValidationError(errors)

    @staticmethod
    def validate_vault_address_for_chain_type(
        *,
        chain_type: ChainType | str,
        address: str,
    ) -> None:
        if chain_type == ChainType.EVM:
            if not Web3.is_checksum_address(address):
                raise ValidationError(_("EVM 收款归集地址必须是 checksum 地址。"))
            return
        if chain_type == ChainType.TRON:
            if not TronAddressCodec.is_valid_base58(address):
                raise ValidationError(_("Tron 收款归集地址必须是 Base58 地址。"))
            return
        raise ValidationError(_("不支持的链类型: %(chain_type)s") % {"chain_type": chain_type})

    @staticmethod
    def vault_field_for_chain_type(chain_type: ChainType | str) -> str:
        if chain_type == ChainType.EVM:
            return "evm_vault"
        if chain_type == ChainType.TRON:
            return "tron_vault"
        raise ValueError(f"unsupported vault chain_type={chain_type}")

    def vault_address_for_chain_type(self, chain_type: ChainType | str) -> str | None:
        return getattr(self, self.vault_field_for_chain_type(chain_type))

    @classmethod
    def retrieve(cls, appid: str):
        try:
            return cls.objects.get(appid=appid)
        except cls.DoesNotExist:
            return None

    @property
    def is_ready(self) -> tuple[bool, list[str]]:
        # 错误项采用统一的"短名词 + 状态"格式，便于前端横排拼接。
        errors: list[str] = []
        if (
            self.evm_invoice_receiving_mode == InvoiceReceivingMode.VaultSlot
            and not self.evm_vault
        ):
            errors.append(_("EVM 金库地址未配置"))  # noqa
        if (
            self.tron_invoice_receiving_mode == InvoiceReceivingMode.VaultSlot
            and not self.tron_vault
        ):
            errors.append(_("Tron 金库地址未配置"))  # noqa
        if not self.ip_white_list:
            errors.append(_("IP 白名单未配置"))  # noqa
        if not self.webhook:
            errors.append(_("通知地址未配置"))  # noqa

        return (not errors), errors


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
