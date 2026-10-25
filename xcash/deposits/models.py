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
        on_delete=models.PROTECT,
        verbose_name=_("链上转账"),
    )
    worth = models.DecimalField(
        _("价值(USD)"),
        max_digits=16,
        decimal_places=6,
        default=0,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("充值收款")
        verbose_name_plural = _("充值收款")

    def __str__(self) -> str:
        return f"Deposit({self.sys_no}, confirmed={self.confirmed})"

    @property
    def confirmed(self) -> bool:
        # Deposit 与其 Transfer 完全同步、共存亡，确认状态直接取自链上转账，
        # 不再维护独立状态机，避免双份真相。
        from chains.models import TransferStatus

        return self.transfer.status == TransferStatus.CONFIRMED
