from decimal import Decimal

from django.test import SimpleTestCase
from django.utils import timezone
from internal_api.serializers.invoices import InternalInvoiceCreateSerializer
from internal_api.serializers.invoices import InternalInvoiceDetailSerializer
from invoices.models import Invoice
from invoices.models import InvoiceBillingMode
from invoices.models import InvoiceProtocol


class InternalInvoiceDurationValidationTests(SimpleTestCase):
    """内部 API 账单有效期边界测试。"""

    def test_duration_over_thirty_minutes_is_rejected(self):
        serializer = InternalInvoiceCreateSerializer(
            data={
                "out_no": "internal-duration-order",
                "title": "Internal duration",
                "currency": "USD",
                "amount": Decimal("10"),
                "methods": {"ETH": ["eth"]},
                "duration": 31,
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("duration", serializer.errors)


class InternalInvoiceDetailSerializerTests(SimpleTestCase):
    """内部 API 账单详情字段测试。"""

    def test_detail_includes_billing_mode_and_protocol(self):
        invoice = Invoice(
            sys_no="INV-test",
            out_no="internal-detail-order",
            title="Internal detail",
            currency="USD",
            amount=Decimal("10"),
            methods={},
            expires_at=timezone.now(),
            billing_mode=InvoiceBillingMode.CONTRACT,
            protocol=InvoiceProtocol.EPAY_V1,
        )

        data = InternalInvoiceDetailSerializer(invoice).data

        self.assertEqual(data["billing_mode"], InvoiceBillingMode.CONTRACT)
        self.assertEqual(data["protocol"], InvoiceProtocol.EPAY_V1)
