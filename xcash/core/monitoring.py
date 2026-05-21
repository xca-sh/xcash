from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Count
from django.db.models import Q

if TYPE_CHECKING:
    from datetime import timedelta
from django.utils import timezone

from core.runtime_settings import get_confirming_withdrawal_timeout
from core.runtime_settings import get_deposit_collection_timeout
from core.runtime_settings import get_pending_withdrawal_timeout
from core.runtime_settings import get_reviewing_withdrawal_timeout
from core.runtime_settings import get_webhook_event_timeout
from deposits.models import Deposit
from deposits.models import DepositStatus
from webhooks.models import WebhookEvent
from withdrawals.models import Withdrawal
from withdrawals.models import WithdrawalStatus


class OperationalRiskService:
    """统一收口后台巡检阈值，避免仪表盘与异步巡检出现两套口径。"""

    @classmethod
    def reviewing_withdrawal_timeout(cls) -> timedelta:
        return get_reviewing_withdrawal_timeout()

    @classmethod
    def pending_withdrawal_timeout(cls) -> timedelta:
        return get_pending_withdrawal_timeout()

    @classmethod
    def confirming_withdrawal_timeout(cls) -> timedelta:
        return get_confirming_withdrawal_timeout()

    @classmethod
    def deposit_collection_timeout(cls) -> timedelta:
        return get_deposit_collection_timeout()

    @classmethod
    def webhook_event_timeout(cls) -> timedelta:
        return get_webhook_event_timeout()

    @classmethod
    def stalled_withdrawals(cls):
        now = timezone.now()
        return Withdrawal.objects.filter(
            Q(
                status=WithdrawalStatus.REVIEWING,
                updated_at__lte=now - cls.reviewing_withdrawal_timeout(),
            )
            | Q(
                status=WithdrawalStatus.PENDING,
                updated_at__lte=now - cls.pending_withdrawal_timeout(),
            )
            | Q(
                status=WithdrawalStatus.CONFIRMING,
                updated_at__lte=now - cls.confirming_withdrawal_timeout(),
            )
        ).select_related("project", "crypto", "chain")

    @classmethod
    def stalled_deposit_collections(cls):
        # 卡单判断基于 DepositCollection.updated_at（归集记录最后活跃时间），
        # 而非 Deposit.updated_at，以准确反映归集链路的真实卡顿时长。
        now = timezone.now()
        return Deposit.objects.filter(
            status=DepositStatus.COMPLETED,
            collection__isnull=False,
            collection__collected_at__isnull=True,
            collection__updated_at__lte=now - cls.deposit_collection_timeout(),
        ).select_related("customer__project", "transfer__chain", "transfer__crypto")

    @classmethod
    def stalled_webhook_events(cls):
        now = timezone.now()
        return WebhookEvent.objects.filter(
            status=WebhookEvent.Status.PENDING,
            created_at__lte=now - cls.webhook_event_timeout(),
        ).select_related("project")

    @classmethod
    def stalled_contract_collections(cls):
        """识别已完成付款但 collector 部署连续失败的合约账单。"""
        from evm.models import ContractDeployCollectionStatus
        from invoices.models import Invoice
        from invoices.models import InvoiceBillingMode
        from invoices.models import InvoiceStatus

        return (
            Invoice.objects.filter(
                billing_mode=InvoiceBillingMode.CONTRACT,
                status=InvoiceStatus.COMPLETED,
            )
            .annotate(
                failed_count=Count(
                    "pay_slots__contract_deploy_collections",
                    filter=Q(
                        pay_slots__contract_deploy_collections__status__in=[
                            ContractDeployCollectionStatus.FAILED,
                            ContractDeployCollectionStatus.DROPPED,
                        ]
                    ),
                ),
                active_count=Count(
                    "pay_slots__contract_deploy_collections",
                    filter=Q(
                        pay_slots__contract_deploy_collections__status__in=[
                            ContractDeployCollectionStatus.CREATED,
                            ContractDeployCollectionStatus.BROADCASTED,
                            ContractDeployCollectionStatus.CONFIRMED,
                        ]
                    ),
                ),
            )
            .filter(failed_count__gte=3, active_count=0)
            .select_related("project")
        )

    @classmethod
    def build_summary(cls, *, limit: int = 4) -> dict:
        """返回后台展示与异步巡检共享的异常概览。"""
        stalled_withdrawals = cls.stalled_withdrawals()
        stalled_deposit_collections = cls.stalled_deposit_collections()
        stalled_webhook_events = cls.stalled_webhook_events()

        return {
            "reviewing_withdrawal_count": stalled_withdrawals.filter(
                status=WithdrawalStatus.REVIEWING
            ).count(),
            "pending_withdrawal_count": stalled_withdrawals.filter(
                status=WithdrawalStatus.PENDING
            ).count(),
            "confirming_withdrawal_count": stalled_withdrawals.filter(
                status=WithdrawalStatus.CONFIRMING
            ).count(),
            "stalled_withdrawal_count": stalled_withdrawals.count(),
            "stalled_deposit_collection_count": stalled_deposit_collections.count(),
            "stalled_webhook_event_count": stalled_webhook_events.count(),
            "recent_stalled_withdrawals": list(
                stalled_withdrawals.order_by("updated_at")[:limit]
            ),
            "recent_stalled_deposit_collections": list(
                stalled_deposit_collections.order_by("updated_at")[:limit]
            ),
            "recent_stalled_webhook_events": list(
                stalled_webhook_events.order_by("created_at")[:limit]
            ),
        }
