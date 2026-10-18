from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from shortuuid.django_fields import ShortUUIDField
from web3 import Web3

from chains.models import ChainType
from common.consts import UPPER_ALPHABET
from common.fields import AddressField
from common.tron_codec import TronAddressCodec


class InvoiceReceivingMode(models.TextChoices):
    VaultSlot = "vault_slot", _("智能合约")
    Differ = "differ", _("钱包直收")


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
    fast_confirm_threshold = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("10"),
        verbose_name=_("快速确认阈值（USD）"),
        help_text=_("低于该金额的账单收款无需等待区块确认数，立即确认"),
    )
    # AML 筛查参数按项目配置：是否筛查、多大金额才值得付费查询，取决于商户自身业务。
    aml_screening_enabled = models.BooleanField(
        _("开启 AML 筛查"),
        default=False,
        help_text=_("开启后对高于 AML 查询阈值的账单收款和充值收款查询付款地址风险。"),
    )
    aml_screening_threshold_usd = models.DecimalField(
        _("AML 查询阈值(USD)"),
        max_digits=16,
        decimal_places=2,
        default=Decimal("100"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text=_("账单收款或充值收款价值大于该阈值时执行 AML 查询。"),
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
            "用于生成 EVM 智能合约收款地址的不可变归集地址，必须是 EVM checksum 地址。"
            "留空时禁止在 EVM 使用智能合约收款；一旦设置不可修改。"
        ),
        unique=True,
    )
    tron_vault = AddressField(
        _("Tron 收款归集地址"),
        null=True,
        blank=True,
        help_text=_(
            "用于生成 Tron 智能合约收款地址的不可变归集地址，必须是 Tron Base58 地址。"
            "留空时禁止在 Tron 使用智能合约收款；"
            "一旦设置不可修改。"
        ),
        unique=True,
    )
    # 账单收款模式按链独立配置。EVM 与 Tron 都有明确值，不再通过全局字段兜底。
    evm_invoice_receiving_mode = models.CharField(
        _("EVM 账单收款模式"),
        choices=InvoiceReceivingMode,
        default=InvoiceReceivingMode.Differ,
        help_text=_("EVM 网络账单收款使用钱包直收或智能合约模式。"),
    )
    tron_invoice_receiving_mode = models.CharField(
        _("Tron 账单收款模式"),
        choices=InvoiceReceivingMode,
        default=InvoiceReceivingMode.Differ,
        help_text=_("Tron 网络账单收款使用钱包直收或智能合约模式。"),
    )
    # 智能合约收款总开关，只在配置入口（admin clean / SaaS API）把关，运行时逻辑不读它。
    # 由 validate_contract_receiving 的不变量保证：关闭 ⇒ 无归集地址 ⇒ 账单只能钱包直收、无法充值。
    contract_receiving_enabled = models.BooleanField(
        _("智能合约收款"),
        default=False,
        help_text=_(
            "开启后才能设置收款归集地址、使用智能合约账单收款与用户充值；"
            "设置任一收款归集地址后不可关闭。"
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
        self.validate_contract_receiving()

    def validate_contract_receiving(self) -> None:
        """校验智能合约收款开关与归集地址、账单收款模式之间的配置不变量。

        只在配置入口调用（admin 表单经 clean()，SaaS API 经序列化器），运行时不依赖它：
        1. 开关关闭 ⇒ evm_vault、tron_vault 都为空。
           违反时按实际改动的字段报错：有归集地址已落库，说明是在关闭开关，报在开关字段；
           归集地址都是本次新填的，报在这些归集地址字段上。admin 中已设置的归集地址是
           只读字段，错误不能挂在它上面，这样区分也保证错误总落在表单可编辑的字段上。
        2. 某链账单收款模式为智能合约 ⇒ 开关已开启且该链归集地址已设置。
        由此关闭状态下两条链必然都是钱包直收，且没有归集地址就无法创建充值收款地址。
        """
        errors = {}
        set_vault_fields = [
            field for field in ("evm_vault", "tron_vault") if getattr(self, field)
        ]
        if not self.contract_receiving_enabled and set_vault_fields:
            persisted = {}
            if self.pk is not None:
                persisted = (
                    Project.objects.filter(pk=self.pk)
                    .values("evm_vault", "tron_vault")
                    .first()
                    or {}
                )
            newly_set_fields = [
                field
                for field in set_vault_fields
                if persisted.get(field) != getattr(self, field)
            ]
            if len(newly_set_fields) < len(set_vault_fields):
                errors["contract_receiving_enabled"] = _(
                    "已设置收款归集地址，不能关闭智能合约收款。"
                )
            else:
                for field in newly_set_fields:
                    errors[field] = _("未开启智能合约收款，不能设置收款归集地址。")

        for chain_type, missing_vault_message in (
            (ChainType.EVM, _("需先设置 EVM 收款归集地址。")),
            (ChainType.TRON, _("需先设置 Tron 收款归集地址。")),
        ):
            mode_field = self.invoice_receiving_mode_field_for_chain_type(chain_type)
            if getattr(self, mode_field) != InvoiceReceivingMode.VaultSlot:
                continue
            if not self.contract_receiving_enabled:
                errors[mode_field] = _("未开启智能合约收款，账单只能使用钱包直收。")
            elif not self.vault_address_for_chain_type(chain_type):
                errors[mode_field] = missing_vault_message
        if errors:
            raise ValidationError(errors)

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
                self.validate_vault_address_is_globally_unique(
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
        raise ValidationError(
            _("不支持的链类型: %(chain_type)s") % {"chain_type": chain_type}
        )

    def validate_vault_address_is_globally_unique(self, *, address: str) -> None:
        project_conflicts = Project.objects.filter(
            Q(evm_vault=address) | Q(tron_vault=address)
        )
        if self.pk:
            project_conflicts = project_conflicts.exclude(pk=self.pk)
        if project_conflicts.exists():
            raise ValidationError(_("收款归集地址已被其他项目地址占用。"))

        from invoices.models import DifferRecipientAddress

        if DifferRecipientAddress.objects.filter(address=address).exists():
            raise ValidationError(_("收款归集地址已被钱包直收地址占用。"))

    @staticmethod
    def vault_field_for_chain_type(chain_type: ChainType | str) -> str:
        if chain_type == ChainType.EVM:
            return "evm_vault"
        if chain_type == ChainType.TRON:
            return "tron_vault"
        raise ValueError(f"unsupported vault chain_type={chain_type}")

    def vault_address_for_chain_type(self, chain_type: ChainType | str) -> str | None:
        return getattr(self, self.vault_field_for_chain_type(chain_type))

    @staticmethod
    def invoice_receiving_mode_field_for_chain_type(
        chain_type: ChainType | str,
    ) -> str:
        """返回某链类型的账单收款模式字段名；未知链类型显式拒绝。"""
        field = {
            ChainType.EVM: "evm_invoice_receiving_mode",
            ChainType.TRON: "tron_invoice_receiving_mode",
        }.get(chain_type)
        if field is None:
            raise ValueError(f"unsupported chain type: {chain_type}")
        return field

    def resolved_invoice_receiving_mode(self, chain_type: ChainType | str) -> str:
        """该链类型实际生效的账单收款模式。"""
        field = self.invoice_receiving_mode_field_for_chain_type(chain_type)
        return getattr(self, field)

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
        from invoices.models import DifferRecipientAddress

        has_differ_address = DifferRecipientAddress.objects.filter(
            project=self,
            active=True,
        ).exists()
        if not has_differ_address and not self.evm_vault and not self.tron_vault:
            errors.append(_("钱包未配置"))  # noqa
        if (
            self.resolved_invoice_receiving_mode(ChainType.EVM)
            == InvoiceReceivingMode.VaultSlot
            and not self.evm_vault
        ):
            errors.append(_("EVM 归集地址未配置"))  # noqa
        if (
            self.resolved_invoice_receiving_mode(ChainType.TRON)
            == InvoiceReceivingMode.VaultSlot
            and not self.tron_vault
        ):
            errors.append(_("Tron 归集地址未配置"))  # noqa
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

    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("加入时间"))

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
