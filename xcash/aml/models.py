from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


class Provider(models.TextChoices):
    QUICKNODE_MISTTRACK = "quicknode_misttrack", _("QuickNode MistTrack")
    MISTTRACK_OPENAPI = "misttrack_openapi", _("MistTrack OpenAPI")


class RiskLevel(models.TextChoices):
    LOW = "Low", _("Low")
    MODERATE = "Moderate", _("Moderate")
    HIGH = "High", _("High")
    SEVERE = "Severe", _("Severe")


class RiskAssessment(models.Model):
    class Status(models.TextChoices):
        SUCCESS = "success", _("查询成功")
        FAILED = "failed", _("查询失败")

    class TargetType(models.TextChoices):
        INVOICE = "invoice", _("账单收款")
        DEPOSIT = "deposit", _("账户充值")

    source = models.CharField(
        _("数据来源"),
        choices=Provider,
        max_length=32,
        default=Provider.QUICKNODE_MISTTRACK,
        db_index=True,
    )
    status = models.CharField(
        _("查询状态"),
        choices=Status,
        max_length=16,
        db_index=True,
    )
    target_type = models.CharField(
        _("目标类型"),
        choices=TargetType,
        max_length=16,
        db_index=True,
    )
    invoice = models.OneToOneField(
        "invoices.Invoice",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="aml_assessment",
        verbose_name=_("账单收款"),
    )
    deposit = models.OneToOneField(
        "deposits.Deposit",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="aml_assessment",
        verbose_name=_("账户充值"),
    )
    address = models.CharField(_("查询地址"), max_length=128, db_index=True)
    tx_hash = models.CharField(_("交易哈希"), max_length=128, blank=True, default="")
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
    raw_response = models.JSONField(_("原始响应摘要"), default=dict, blank=True)
    error_message = models.TextField(_("错误摘要"), blank=True, default="")
    checked_at = models.DateTimeField(_("查询完成时间"), null=True, blank=True)
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        verbose_name = _("AML 评估")
        verbose_name_plural = _("AML 评估")
        constraints = [
            models.CheckConstraint(
                name="aml_assessment_exactly_one_target",
                condition=(
                    (
                        models.Q(invoice__isnull=False)
                        & models.Q(deposit__isnull=True)
                    )
                    | (
                        models.Q(invoice__isnull=True)
                        & models.Q(deposit__isnull=False)
                    )
                ),
            ),
            models.CheckConstraint(
                name="aml_assessment_target_type_matches_target",
                condition=(
                    (
                        models.Q(target_type="invoice")
                        & models.Q(invoice__isnull=False)
                        & models.Q(deposit__isnull=True)
                    )
                    | (
                        models.Q(target_type="deposit")
                        & models.Q(invoice__isnull=True)
                        & models.Q(deposit__isnull=False)
                    )
                ),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_target_type_display()} {self.address}"

    def clean(self):
        super().clean()
        has_invoice = self.invoice_id is not None
        has_deposit = self.deposit_id is not None
        if has_invoice == has_deposit:
            raise ValidationError(_("AML 评估必须且只能关联一个业务目标。"))
