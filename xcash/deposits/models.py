from aml.models import RiskLevel
from django.db import models
from django.utils.translation import gettext_lazy as _

from common.fields import SysNoField


class Deposit(models.Model):
    sys_no = SysNoField(prefix="DXC")
    customer = models.ForeignKey(
        "projects.Customer",
        on_delete=models.PROTECT,
        verbose_name=_("客户"),
    )
    transfer = models.OneToOneField(
        "chains.Transfer",
        on_delete=models.CASCADE,
        verbose_name=_("链上转账"),
    )
    worth = models.DecimalField(
        _("价值(USD)"),
        max_digits=16,
        decimal_places=6,
        default=0,
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("账户充值")
        verbose_name_plural = _("账户充值")

    def __str__(self) -> str:
        return f"Deposit({self.sys_no}, confirmed={self.confirmed})"

    @property
    def confirmed(self) -> bool:
        # Deposit 与其 Transfer 完全同步、共存亡，确认状态直接取自链上转账，
        # 不再维护独立状态机，避免双份真相。
        from chains.models import TransferStatus

        return self.transfer.status == TransferStatus.CONFIRMED

    @property
    def content(self):
        from deposits.service import DepositService

        return DepositService.build_webhook_payload(self)
