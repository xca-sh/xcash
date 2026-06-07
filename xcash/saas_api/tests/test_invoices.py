from decimal import Decimal

from django.test import SimpleTestCase
from django.utils import timezone
from saas_api.serializers.invoices import SaasInvoiceDetailSerializer
from saas_api.viewsets.invoices import SaasInvoiceViewSet

from invoices.models import Invoice
from invoices.models import InvoiceProtocol


class SaasInvoiceCreateDeprecatedTests(SimpleTestCase):
    """内部 API 不再承担 Invoice 创建职责。"""

    def test_saas_invoice_api_disables_post(self):
        self.assertNotIn("post", SaasInvoiceViewSet.http_method_names)


class SaasInvoiceDetailSerializerTests(SimpleTestCase):
    """内部 API 账单详情字段测试。"""

    def test_detail_includes_protocol(self):
        invoice = Invoice(
            sys_no="INV-test",
            out_no="saas-detail-order",
            title="SaaS detail",
            currency="USD",
            amount=Decimal("10"),
            methods={},
            expires_at=timezone.now(),
            protocol=InvoiceProtocol.EPAY_V1,
        )

        data = SaasInvoiceDetailSerializer(invoice).data

        self.assertEqual(data["protocol"], InvoiceProtocol.EPAY_V1)
