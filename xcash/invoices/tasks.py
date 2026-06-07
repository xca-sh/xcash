from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import Invoice
from .models import InvoiceStatus


@shared_task
def check_expired(instance_id: int):
    # 无锁预检避免不必要的事务开销。
    invoice = Invoice.objects.get(id=instance_id)
    if invoice.status != InvoiceStatus.WAITING or invoice.transfer_id is not None:
        return

    expired_at = timezone.now()
    with transaction.atomic():
        # 只过期尚未观察到付款的账单；已绑定 Transfer 的 WAITING 账单
        # 表示支付页正在等待链上确认，不能因确认窗口跨过 expires_at 而回退为 EXPIRED。
        # 注意：Celery ETA 不是硬性保证（worker 重启/broker 故障可能导致提前执行），
        # 因此必须在锁住后校验 expires_at 已到达，避免误判有效账单为过期。
        locked = (
            Invoice.objects.select_for_update()
            .filter(
                pk=invoice.pk,
                status=InvoiceStatus.WAITING,
                transfer__isnull=True,
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


@shared_task
def fallback_invoice_expired():
    now = timezone.now()
    # 批量收集需要过期的账单 ID，避免逐条处理的 N+1 问题。
    expired_ids = list(
        Invoice.objects.filter(
            status=InvoiceStatus.WAITING,
            transfer__isnull=True,
            expires_at__lte=now,
        ).values_list("pk", flat=True)
    )
    if not expired_ids:
        return

    with transaction.atomic():
        # 加锁顺序必须与 try_match_invoice 一致：只锁 Invoice，且按 pk 固定顺序。
        # order_by("pk") 保证多行锁定顺序一致，避免两个并发 fallback 任务死锁。
        locked_ids = list(
            Invoice.objects.select_for_update()
            .filter(
                pk__in=expired_ids,
                status=InvoiceStatus.WAITING,
                transfer__isnull=True,
            )
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        if not locked_ids:
            return

        Invoice.objects.filter(
            pk__in=locked_ids,
            status=InvoiceStatus.WAITING,
            transfer__isnull=True,
        ).update(
            status=InvoiceStatus.EXPIRED,
            updated_at=now,
        )
