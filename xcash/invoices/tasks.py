from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from evm.models import ContractDeployCollectionStatus

from .models import Invoice
from .models import InvoiceBillingMode
from .models import InvoicePaySlot
from .models import InvoicePaySlotDiscardReason
from .models import InvoicePaySlotStatus
from .models import InvoiceStatus


@shared_task
def check_expired(instance_id: int):
    # 无锁预检避免不必要的事务开销。
    invoice = Invoice.objects.get(id=instance_id)
    if invoice.status != InvoiceStatus.WAITING:
        return

    expired_at = timezone.now()
    with transaction.atomic():
        # 先锁 Invoice 行，保持与 try_match_invoice 一致的加锁顺序（Invoice → PaySlot），
        # 同时保证 Invoice 和 PaySlot 的状态变更在同一事务中原子完成。
        # 注意：Celery ETA 不是硬性保证（worker 重启/broker 故障可能导致提前执行），
        # 因此必须在锁住后校验 expires_at 已到达，避免误判有效账单为过期。
        locked = (
            Invoice.objects.select_for_update()
            .filter(
                pk=invoice.pk,
                status=InvoiceStatus.WAITING,
                expires_at__lte=expired_at,
            )
            .first()
        )
        if locked is None:
            return

        Invoice.objects.filter(pk=invoice.pk).update(
            status=InvoiceStatus.EXPIRED,
            updated_at=expired_at,
        )
        # 账单过期后立即释放活跃槽位，避免地址/金额组合被数据库唯一约束永久占住。
        InvoicePaySlot.objects.filter(
            invoice=invoice,
            status=InvoicePaySlotStatus.ACTIVE,
        ).update(
            status=InvoicePaySlotStatus.DISCARDED,
            discard_reason=InvoicePaySlotDiscardReason.EXPIRED,
            discarded_at=expired_at,
            updated_at=expired_at,
        )


@shared_task
def fallback_invoice_expired():
    now = timezone.now()
    # 批量收集需要过期的账单 ID，避免逐条处理的 N+1 问题。
    expired_ids = list(
        Invoice.objects.filter(
            status=InvoiceStatus.WAITING,
            expires_at__lte=now,
        ).values_list("pk", flat=True)
    )
    if not expired_ids:
        return

    with transaction.atomic():
        # 加锁顺序必须与 try_match_invoice 一致：先锁 Invoice 再处理 PaySlot，
        # 否则并发时会形成 Invoice→PaySlot vs PaySlot→Invoice 的死锁。
        # order_by("pk") 保证多行锁定顺序一致，避免两个并发 fallback 任务死锁。
        locked_ids = list(
            Invoice.objects.select_for_update()
            .filter(pk__in=expired_ids, status=InvoiceStatus.WAITING)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        if not locked_ids:
            return

        # 更新顺序与 check_expired 保持一致：先更新 Invoice 状态，再释放 PaySlot。
        Invoice.objects.filter(
            pk__in=locked_ids,
            status=InvoiceStatus.WAITING,
        ).update(
            status=InvoiceStatus.EXPIRED,
            updated_at=now,
        )
        InvoicePaySlot.objects.filter(
            invoice_id__in=locked_ids,
            status=InvoicePaySlotStatus.ACTIVE,
        ).update(
            status=InvoicePaySlotStatus.DISCARDED,
            discard_reason=InvoicePaySlotDiscardReason.EXPIRED,
            discarded_at=now,
            updated_at=now,
        )


@shared_task
def deploy_contract_collection(invoice_id: int):
    """异步触发合约账单 collector 部署与归集。"""
    try:
        invoice = Invoice.objects.get(pk=invoice_id)
    except Invoice.DoesNotExist:
        return
    invoice.trigger_contract_collection()


@shared_task
def retry_contract_collection_for_completed_invoices():
    """兜底扫描已完成但无活跃归集记录的合约账单并重新入队。"""
    cutoff = timezone.now() - timedelta(minutes=2)
    active_states = (
        ContractDeployCollectionStatus.CREATED,
        ContractDeployCollectionStatus.BROADCASTED,
        ContractDeployCollectionStatus.CONFIRMED,
    )
    candidates = (
        Invoice.objects.filter(
            billing_mode=InvoiceBillingMode.CONTRACT,
            status=InvoiceStatus.COMPLETED,
            updated_at__lte=cutoff,
        )
        .exclude(
            pay_slots__contract_deploy_collections__status__in=active_states,
        )
        .distinct()
        .values_list("pk", flat=True)[:50]
    )
    for invoice_id in candidates:
        deploy_contract_collection.delay(invoice_id)
