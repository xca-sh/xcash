import secrets
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db import models
from django.db import transaction as db_transaction
from django.db.models import Max
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

logger = structlog.get_logger()

from risk.models import RiskLevel

from common.fields import AddressField
from common.fields import SysNoField
from common.permission_check import filter_saas_allowed_methods
from currencies.service import CryptoService
from currencies.service import FiatService
from evm.models import VaultSlot
from evm.models import VaultSlotUsage
from projects.models import Project
from projects.service import ProjectService

from .exceptions import InvoiceAllocationError

if TYPE_CHECKING:
    from chains.models import Chain
    from currencies.models import Crypto


class InvoiceStatus(models.TextChoices):
    WAITING = "waiting", _("待支付")
    CONFIRMING = "confirming", _("确认中")
    COMPLETED = "completed", _("已完成")
    EXPIRED = "expired", _("已超时")


class InvoiceProtocol(models.TextChoices):
    NATIVE = "native", _("Xcash 原生")
    EPAY_V1 = "epay_v1", _("EPay V1")


class InvoiceBillingMode(models.TextChoices):
    DIFFER = "differ", _("差额")
    CONTRACT = "contract", _("合约")


class Invoice(models.Model):
    MAX_ALLOCATION_RETRY = 5

    # 保留类属性别名，使 Invoice.InvoiceAllocationError 继续可用。
    InvoiceAllocationError = InvoiceAllocationError

    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        verbose_name=_("项目"),
    )
    sys_no = SysNoField(prefix="INV")

    out_no = models.CharField(
        verbose_name=_("商户单号"),
        db_index=True,
    )
    title = models.CharField(_("标题"))
    currency = models.CharField(_("计价货币"))
    amount = models.DecimalField(
        verbose_name=_("金额"),
        max_digits=32,
        decimal_places=8,
    )
    methods = models.JSONField(
        default=dict,
        verbose_name=_("支持的支付方式"),
    )

    crypto = models.ForeignKey(
        "currencies.Crypto",
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        verbose_name=_("加密货币"),
    )
    chain = models.ForeignKey(
        "chains.Chain",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        verbose_name=_("链"),
    )
    pay_amount = models.DecimalField(
        verbose_name=_("支付加密货币数量"),
        max_digits=32,
        decimal_places=8,
        blank=True,
        null=True,
        help_text=_("支付加密货币数量可能在原始数量上差额浮动"),
    )
    pay_address = AddressField(
        verbose_name=_("支付地址"),
        blank=True,
        null=True,
        db_index=True,
    )

    started_at = models.DateTimeField(_("支付开始时间"), auto_now_add=True)
    expires_at = models.DateTimeField(_("支付截止时间"))
    notify_url = models.URLField(_("异步通知地址"), blank=True, default="")
    return_url = models.URLField(_("支付成功后同步跳转地址"), blank=True, default="")
    worth = models.DecimalField(
        _("价值(USD)"),
        max_digits=16,
        decimal_places=6,
        default=0,
    )
    transfer = models.OneToOneField(
        "chains.Transfer",
        on_delete=models.SET_NULL,
        verbose_name=_("链上转账"),
        blank=True,
        null=True,
    )
    status = models.CharField(
        choices=InvoiceStatus,
        default=InvoiceStatus.WAITING,
        verbose_name=_("状态"),
    )
    risk_level = models.CharField(  # noqa: DJ001
        _("风险等级"),
        choices=RiskLevel,
        max_length=16,
        null=True,
        blank=True,
        db_index=True,
    )
    risk_score = models.DecimalField(
        _("风险分数"),
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
    )
    protocol = models.CharField(
        choices=InvoiceProtocol,
        default=InvoiceProtocol.NATIVE,
        max_length=16,
        db_index=True,
        verbose_name=_("协议"),
    )
    billing_mode = models.CharField(
        choices=InvoiceBillingMode,
        default=InvoiceBillingMode.DIFFER,
        max_length=16,
        db_index=True,
        verbose_name=_("计费模式"),
    )

    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = _("账单")
        verbose_name_plural = _("账单")
        constraints = [
            # 统一采用具名 UniqueConstraint，便于数据库约束报错定位和后续约束扩展。
            models.UniqueConstraint(
                fields=("project", "out_no"),
                name="uniq_invoice_project_out_no",
            ),
            models.UniqueConstraint(
                fields=("project", "crypto", "chain", "pay_address", "pay_amount"),
                condition=Q(status=InvoiceStatus.WAITING),
                name="uniq_invoice_active_payment",
            ),
        ]

    def __str__(self):
        return f"{self.sys_no}"

    @property
    def is_crypto_fixed(self):
        """是否为固定加密货币模式"""
        return CryptoService.exists(self.currency)

    @classmethod
    def available_methods(
        cls,
        project: Project,
        billing_mode: str = InvoiceBillingMode.DIFFER,
    ) -> dict[str, list[str]]:
        """返回项目在指定 billing_mode 下可用的 crypto→链列表，是「最终 methods」的唯一生成器。

        买家在 select_method 时只校验 (crypto, chain) 是否在 invoice.methods 内、不再按
        billing_mode 复检（select_method 直接按 billing_mode 分配），故这里产出的组合必须是
        该 billing_mode 下真正可付的——否则买家会选中一个无法分配的组合。

        逻辑 = 系统支持的 invoice (crypto, chain) 组合 ∩ 该 billing_mode 下项目可收款的链
              ∩ SaaS 白名单。
        - CONTRACT：仅 EVM 链，且项目已设 vault（见 contract_receivable_chain_codes）。
        - DIFFER：项目已配 DifferRecipientAddress 的 chain_type 对应的链（EVM/Tron 通用）。
        """
        if billing_mode == InvoiceBillingMode.CONTRACT:
            receivable_codes = ProjectService.contract_receivable_chain_codes(project)
        else:
            receivable_codes = ProjectService.differ_receivable_chain_codes(project)

        # allowed_methods 已按 chain_codes=receivable_codes 收敛查询，返回的每个 symbol 的
        # 链集合必为 receivable_codes 子集且非空——故此处无需再做交集或空集过滤。
        allowed = CryptoService.allowed_methods(chain_codes=receivable_codes)

        methods = {
            symbol: sorted(chain_codes) for symbol, chain_codes in allowed.items()
        }

        return filter_saas_allowed_methods(
            appid=project.appid,
            methods=methods,
        )

    def _allocate_differ_payment(
        self,
        crypto: "Crypto",
        chain: "Chain",
        crypto_amount: Decimal,
    ) -> tuple[str, Decimal]:
        """差额账单：寻找空闲的 (pay_address, pay_amount) 支付组合。"""
        detail = (
            f"project={self.project_id}, crypto={crypto.symbol}, chain={chain.code}"
        )
        pay_address, pay_amount = Invoice.get_pay_differ(
            project=self.project,
            crypto=crypto,
            chain=chain,
            crypto_amount=crypto_amount,
        )
        if not (pay_address and pay_amount):
            logger.warning(
                "Invoice pay method allocation failed",
                detail=detail,
            )
            raise self.InvoiceAllocationError(detail)
        return pay_address, pay_amount

    def _has_contract_slot_payment_overlap(
        self,
        *,
        slot: VaultSlot,
        crypto: "Crypto",
        chain: "Chain",
        crypto_amount: Decimal,
    ) -> bool:
        # 同一个合约收款槽位只在"币种 + 金额"重合且对方仍为 WAITING 时不可复用；
        # 不同金额可以复用同一 VaultSlot 地址。
        # 占用判据与 uniq_invoice_active_payment 约束保持一致——只看 status=WAITING，
        # 不叠加 expires_at 过滤：过期账单要等状态翻成 EXPIRED 才真正释放槽位，否则约束
        # 仍锁着该 (pay_address, pay_amount) 组合，而这里若漏判就会让分配陷入
        # IntegrityError 重试死循环（同 get_pay_differ 的处理）。
        return Invoice.objects.filter(
            project=self.project,
            crypto=crypto,
            chain=chain,
            pay_address=slot.address,
            pay_amount=crypto_amount,
            billing_mode=InvoiceBillingMode.CONTRACT,
            status=InvoiceStatus.WAITING,
        ).exists()

    def _get_contract_vault_slot(
        self,
        *,
        crypto: "Crypto",
        chain: "Chain",
        crypto_amount: Decimal,
    ) -> VaultSlot:
        """返回本次合约账单可使用的 INVOICE VaultSlot。"""
        vault_address = self.project.vault
        if not vault_address:
            raise self.InvoiceAllocationError(
                f"project={self.project_id} VaultSlot Vault 地址未配置"
            )

        reusable_slots = VaultSlot.objects.filter(
            project=self.project,
            chain=chain,
            usage=VaultSlotUsage.INVOICE,
        ).order_by("invoice_index", "pk")
        for slot in reusable_slots:
            if not self._has_contract_slot_payment_overlap(
                slot=slot,
                crypto=crypto,
                chain=chain,
                crypto_amount=crypto_amount,
            ):
                db_transaction.on_commit(
                    lambda slot_pk=slot.pk: VaultSlot.schedule_deploy(slot_pk)
                )
                return slot

        latest_index = reusable_slots.aggregate(max_index=Max("invoice_index"))[
            "max_index"
        ]
        invoice_index = 0 if latest_index is None else latest_index + 1
        try:
            VaultSlot.ensure_invoice_address(
                project=self.project,
                chain=chain,
                invoice_index=invoice_index,
            )
        except RuntimeError as exc:
            raise self.InvoiceAllocationError(str(exc)) from exc
        return VaultSlot.objects.get(
            project=self.project,
            chain=chain,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=invoice_index,
        )

    def _allocate_contract_slot(
        self,
        crypto: "Crypto",
        chain: "Chain",
        crypto_amount: Decimal,
    ) -> tuple[str, Decimal]:
        """合约账单：按 XcashVaultSlotFactory 获取本次账单使用的 VaultSlot。"""
        slot = self._get_contract_vault_slot(
            crypto=crypto,
            chain=chain,
            crypto_amount=crypto_amount,
        )
        return slot.address, crypto_amount

    @db_transaction.atomic
    def select_method(self, crypto: "Crypto", chain: "Chain"):
        # 先锁账单行，保证同一账单的多次切链/切币只能留下一个当前支付指引。
        Invoice.objects.select_for_update().get(pk=self.pk)
        self.refresh_from_db()

        if (
            self.crypto_id == crypto.id
            and self.chain_id == chain.id
            and self.pay_address
            and self.pay_amount is not None
        ):
            return True

        if self.is_crypto_fixed:
            crypto_amount = self.amount
        else:
            fiat = FiatService.get_by_code(self.currency)
            crypto_amount = FiatService.to_crypto(
                fiat=fiat, crypto=crypto, amount=self.amount
            )

        detail = (
            f"project={self.project_id}, crypto={crypto.symbol}, chain={chain.code}"
        )

        for _retry in range(self.MAX_ALLOCATION_RETRY):
            try:
                with db_transaction.atomic():
                    if self.billing_mode == InvoiceBillingMode.CONTRACT:
                        pay_address, pay_amount = self._allocate_contract_slot(
                            crypto,
                            chain,
                            crypto_amount,
                        )
                    else:
                        pay_address, pay_amount = self._allocate_differ_payment(
                            crypto,
                            chain,
                            crypto_amount,
                        )

                    created_at = timezone.now()
                    self._set_current_payment(
                        crypto=crypto,
                        chain=chain,
                        pay_address=pay_address,
                        pay_amount=pay_amount,
                        started_at=created_at,
                    )
                    return True
            except IntegrityError:
                logger.warning(
                    "Invoice payment allocation conflicted, retrying",
                    detail=detail,
                )
                continue

        raise self.InvoiceAllocationError(f"{detail} (alloc retry exceeded)")

    @classmethod
    def get_pay_differ(
        cls,
        project: Project,
        crypto: "Crypto",
        chain: "Chain",
        crypto_amount: Decimal,
    ) -> tuple[str | None, Decimal | None]:
        """Differ amount 分配算法：为每张 Invoice 找到一个唯一的 (地址, 金额) 组合。

        核心思路：
        1. 从基础金额起步，以 crypto.differ_step 为单位生成 101 个候选金额（差额 0~100 档），
           浮动范围极小（通常 < 0.001%），对买家感知影响可忽略。
        2. 先遍历金额再遍历地址（双层循环），保证同一账单产生的差额尽量小——
           优先在最小差额下轮换地址，地址不够时才升档。
        3. 并发安全由 Invoice 的部分唯一约束（uniq_invoice_active_payment）保证，
           冲突时由 select_method() 的 IntegrityError 重试循环处理。
           不使用 SELECT FOR UPDATE——悲观锁范围过广（101×N 行）会与 FK 约束检查
           形成环形等待，导致死锁。
        4. 若 101 × N 个组合全部被占用，返回 (None, None) 表示分配失败。
        """
        amounts = [crypto_amount + step * crypto.differ_step for step in range(101)]
        addresses = list(
            ProjectService.invoice_recipient_addresses(
                project,
                chain_type=chain.type,
            )
        )
        if not addresses:
            return None, None

        # 乐观并发：普通 SELECT 读取已占用的当前支付指引，不加行锁。
        # 占用判据必须与 uniq_invoice_active_payment 约束严格一致——只看 status=WAITING，
        # 不能再叠加 expires_at 过滤。约束是 partial unique index，无法引用 now()，因此
        # 凡 status=WAITING 的账单（哪怕已过期但状态尚未被过期任务翻成 EXPIRED）都仍然
        # 锁着其 (pay_address, pay_amount) 组合。若这里按 expires_at 把"过期未翻转"的账单
        # 当成空闲返回，_set_current_payment 的 UPDATE 必然命中约束抛 IntegrityError，
        # 而重试会反复返回同一组合无法前进，最终耗尽 MAX_ALLOCATION_RETRY 抛
        # InvoiceAllocationError——即便仍有大量真正空闲组合也分配不出去。
        # 过期账单的组合由 check_expired / fallback_invoice_expired 把状态翻成 EXPIRED 后
        # 自然释放。并发选中同一空隙的情形仍由约束拦截，外层 select_method() 重试推进。
        existing = set(
            Invoice.objects.filter(
                project=project,
                crypto=crypto,
                chain=chain,
                status=InvoiceStatus.WAITING,
                pay_amount__in=amounts,
                pay_address__in=addresses,
            ).values_list("pay_address", "pay_amount")
        )

        for amount in amounts:
            for address in addresses:
                if (address, amount) not in existing:
                    return address, amount

        return None, None

    @property
    def crypto_address(self):
        if self.crypto:
            return self.crypto.address(self.chain)
        return None

    def _set_current_payment(
        self,
        *,
        crypto: "Crypto",
        chain: "Chain",
        pay_address: str,
        pay_amount: Decimal,
        started_at=None,
    ) -> None:
        # Invoice 当前字段就是唯一支付指引；切换支付方式时直接覆盖旧指引。
        try:
            worth = crypto.to_fiat(
                fiat=FiatService.get_by_code("USD"),
                amount=pay_amount,
            )
        except (KeyError, TypeError):
            # 价格数据不完整时（如新上线币种尚未同步 USD 价格），
            # 降级为 0 而非中断整个匹配/选方式流程。
            logger.warning(
                "crypto price missing for USD",
                crypto=crypto.pk,
            )
            worth = Decimal("0")
        updated_values = {
            "crypto_id": crypto.pk,
            "chain_id": chain.pk,
            "pay_address": pay_address,
            "pay_amount": pay_amount,
            "worth": worth,
            "updated_at": timezone.now(),
        }
        if started_at is not None:
            updated_values["started_at"] = started_at

        Invoice.objects.filter(pk=self.pk).update(**updated_values)
        self.refresh_from_db()


class EpayMerchant(models.Model):
    # pid 分配策略：从 1688 起步，避免与 EPay 生态中常见的小 pid 撞号；
    # 后续每次自动分配都基于当前最大 pid + 1，单调递增。
    PID_BASELINE = 1688
    # 自动生成的 secret_key 字符串长度，恰好满足 EpayMerchantUpdateSerializer
    # 的 16~128 校验区间下限。
    SECRET_KEY_LENGTH = 16

    project = models.OneToOneField(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="epay_merchant",
        verbose_name=_("项目"),
    )
    pid = models.PositiveBigIntegerField(_("EPay 商户 ID"), unique=True)
    secret_key = models.CharField(
        _("EPay 密钥"),
        max_length=128,
        help_text=_(
            "EPay 协议签名密钥。建议使用强随机字符串，不要与项目 HMAC 密钥重用。"
        ),
    )
    active = models.BooleanField(_("启用"), default=True)
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)

    class Meta:
        verbose_name = _("EPay 商户")
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.pid} / {self.project_id}"

    @property
    def signing_key(self) -> str:
        """EPay 协议签名密钥（独立于项目 HMAC 密钥）。"""
        # 纵深防御：即使 admin 表单和 migration 0005 都拦截了空 secret_key，
        # fixtures、bulk_create、test fixture 等仍可能绕过校验直接写入空串。
        # 此处 fail-fast，避免下游用空 KEY 算签名导致任何人都能伪造合法 sign。
        if not self.secret_key:
            raise ValueError(
                f"EpayMerchant(pid={self.pid}) secret_key 为空，"
                "无法用于 EPay 协议签名。"
            )
        return self.secret_key

    @classmethod
    def _generate_secret_key(cls) -> str:
        # token_urlsafe(12) 稳定产出 16 位 base64url 字符串；
        # 加密强度由 secrets 模块保证，无需手工加盐。
        return secrets.token_urlsafe(12)

    @classmethod
    def _allocate_pid(cls) -> int:
        # 表为空 / 最大值仍低于 baseline 时取 baseline，否则单调递增。
        # 调用方负责持有事务锁，避免两个并发请求拿到相同 pid 后再都 INSERT。
        max_pid = cls.objects.aggregate(Max("pid"))["pid__max"]
        if max_pid is None or max_pid < cls.PID_BASELINE:
            return cls.PID_BASELINE
        return max_pid + 1

    @classmethod
    def ensure_for_project(cls, project) -> "EpayMerchant":
        """幂等地为 project 拿到 EpayMerchant：存在则返回，不存在则系统级 lazy 创建。

        商户级配置由系统接管：用户既不指定 pid 也不指定 secret_key，仅能后续
        修改 active / secret_key。并发创建依赖 pid unique 兜底，IntegrityError
        后重试，最多 5 次（实际并发量极低，5 次已足够）。
        """
        existing = cls.objects.filter(project=project).first()
        if existing is not None:
            return existing

        for _attempt in range(5):
            try:
                with db_transaction.atomic():
                    return cls.objects.create(
                        project=project,
                        pid=cls._allocate_pid(),
                        secret_key=cls._generate_secret_key(),
                        active=True,
                    )
            except IntegrityError:
                # 两种可能：(a) pid 与并发请求撞号、(b) 另一个并发请求已为本 project 建好
                # OneToOne。前者重新计算 pid 再尝试；后者直接返回已存在的记录。
                existing = cls.objects.filter(project=project).first()
                if existing is not None:
                    return existing

        raise RuntimeError(
            f"Failed to allocate EpayMerchant for project {project.pk} after retries"
        )


class EpayOrder(models.Model):
    invoice = models.OneToOneField(
        "invoices.Invoice",
        on_delete=models.CASCADE,
        related_name="epay_order",
        verbose_name=_("账单"),
    )
    merchant = models.ForeignKey(
        "invoices.EpayMerchant",
        on_delete=models.PROTECT,
        related_name="orders",
        verbose_name=_("EPay 商户"),
    )
    pid = models.CharField(_("EPay 商户 ID"), max_length=32)
    trade_no = models.CharField(_("EPay 平台订单号"), max_length=64, db_index=True)
    out_trade_no = models.CharField(_("商户订单号"), max_length=64, db_index=True)
    type = models.CharField(_("支付类型"), max_length=32, blank=True, default="")
    name = models.CharField(_("商品名称"), max_length=128)
    money = models.DecimalField(_("订单金额"), max_digits=32, decimal_places=2)
    notify_url = models.URLField(_("异步通知地址"))
    return_url = models.URLField(_("同步跳转地址"), blank=True, default="")
    param = models.CharField(_("业务扩展参数"), max_length=512, blank=True, default="")
    sign_type = models.CharField(_("签名类型"), max_length=16, default="MD5")
    raw_request = models.JSONField(_("原始请求"), default=dict)
    notify_event = models.OneToOneField(
        "webhooks.WebhookEvent",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="+",
        verbose_name=_("通知事件"),
    )
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)

    class Meta:
        verbose_name = _("EPay 订单")
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=("merchant", "out_trade_no"),
                name="uniq_epay_order_merchant_out_trade_no",
            ),
            models.UniqueConstraint(
                fields=("merchant", "trade_no"),
                name="uniq_epay_order_merchant_trade_no",
            ),
        ]

    def __str__(self) -> str:
        return self.trade_no

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        errors = {}

        if self.invoice_id and self.merchant_id:
            if self.invoice.project_id != self.merchant.project_id:
                errors["invoice"] = _("账单项目必须与 EPay 商户项目一致。")

        if self.merchant_id and self.pid != str(self.merchant.pid):
            errors["pid"] = _("EPay 商户 ID 必须与所属商户一致。")

        if errors:
            raise ValidationError(errors)
