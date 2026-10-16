from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class Provider(models.TextChoices):
    QUICKNODE_MISTTRACK = "quicknode_misttrack", _("QuickNode MistTrack")
    # 已停用的数据源，仅保留以正确展示历史 RiskAssessment 记录。
    MISTTRACK_OPENAPI = "misttrack_openapi", _("MistTrack OpenAPI")


class RiskLevel(models.TextChoices):
    LOW = "Low", _("Low")
    MODERATE = "Moderate", _("Moderate")
    HIGH = "High", _("High")
    SEVERE = "Severe", _("Severe")


class RiskAssessment(models.Model):
    """一笔入账转账的付款地址 AML 评估。

    挂在 Transfer 上而不是账单/充值上：转账一匹配为业务收款就开始查询，与区块确认并行，
    此时充值记录尚未创建；确认时账单/充值再从 Transfer 读取结果写入自身快照与 webhook。
    """

    class Status(models.TextChoices):
        SUCCESS = "success", _("查询成功")
        FAILED = "failed", _("查询失败")

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
    transfer = models.OneToOneField(
        "chains.Transfer",
        on_delete=models.CASCADE,
        # 仅存量迁移中无法关联到 Transfer 的历史记录为空；新记录总是有值
        null=True,
        blank=True,
        related_name="aml_assessment",
        verbose_name=_("转账"),
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

    def __str__(self) -> str:
        return self.address


def transfer_assessment(transfer) -> RiskAssessment | None:
    """读取收款转账的 AML 评估；转账为空或未筛查时返回 None。

    走 transfer 的反向访问器，列表页配合 select_related("transfer__aml_assessment")
    不会逐行补查。
    """
    if transfer is None:
        return None
    try:
        return transfer.aml_assessment
    except RiskAssessment.DoesNotExist:
        return None
