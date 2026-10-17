from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import Invoice
from .models import InvoiceStatus


@shared_task
def fallback_invoice_expired():
    """周期批量过期到期未付的账单，是账单过期的唯一机制。

    不再为每张账单投递 ETA 任务：过期与支付组合槽位释放最多延后一个周期（15s）。
    只过期尚未观察到付款的账单；已绑定 Transfer 的 WAITING 账单表示支付页正在等待
    链上确认，不能因确认窗口跨过 expires_at 而回退为 EXPIRED。expires_at 只在建单时
    写入、此后不会延长，因此锁内无需再复核到期时间。
    """
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
