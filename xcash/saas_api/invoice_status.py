"""SaaS 商户视角的账单状态。

Invoice 只有 waiting / completed / expired 三个状态：链上付款被识别后只绑定 Transfer，
账单状态保持不变，直到 Transfer.confirm() 才翻成 COMPLETED。商户需要看到「钱已到、在等
区块确认」，因此在 SaaS API 层把「已绑定且转账仍在确认中的待支付/已超时账单」单独归为
confirming，并从 waiting / expired 中剔除。已超时账单也算，是因为截止前付的款确认后
仍会完成（InvoiceService.confirm_invoice 接受 EXPIRED）。

列表筛选与状态计数共用这里的条件，保证页签数字与筛选结果一致，四类互斥且覆盖全部账单。
"""

from django.db.models import Q

from chains.models import TransferStatus
from invoices.models import InvoiceStatus

CONFIRMING = Q(
    status__in=[InvoiceStatus.WAITING, InvoiceStatus.EXPIRED],
    transfer__status=TransferStatus.CONFIRMING,
)

DISPLAY_STATUS_FILTERS: dict[str, Q] = {
    "waiting": Q(status=InvoiceStatus.WAITING) & ~CONFIRMING,
    "confirming": CONFIRMING,
    "completed": Q(status=InvoiceStatus.COMPLETED),
    "expired": Q(status=InvoiceStatus.EXPIRED) & ~CONFIRMING,
}


def display_status(invoice) -> str:
    """单笔账单的商户可见状态，口径与 DISPLAY_STATUS_FILTERS 一致（导出用）。"""
    transfer = invoice.transfer
    if (
        invoice.status in (InvoiceStatus.WAITING, InvoiceStatus.EXPIRED)
        and transfer is not None
        and transfer.status == TransferStatus.CONFIRMING
    ):
        return "confirming"
    return invoice.status
