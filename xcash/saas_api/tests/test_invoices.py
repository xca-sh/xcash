from decimal import Decimal

import pytest
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from saas_api.serializers.invoices import SaasInvoiceDetailSerializer
from saas_api.viewsets.invoices import SaasInvoiceViewSet

from chains.constants import ChainCode
from chains.models import Transfer
from chains.models import TransferStatus
from chains.tests_fixtures import make_evm_chain
from currencies.models import Crypto
from currencies.models import Fiat
from invoices.models import Invoice
from invoices.models import InvoiceProtocol
from projects.models import Project

AUTH = "Bearer test-saas-token"


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
            currency_id="USD",
            amount=Decimal("10"),
            methods={},
            expires_at=timezone.now(),
            protocol=InvoiceProtocol.EPAY_V1,
        )

        data = SaasInvoiceDetailSerializer(invoice).data

        self.assertEqual(data["protocol"], InvoiceProtocol.EPAY_V1)


@pytest.mark.django_db
class TestSaasInvoiceListQueries:
    """列表接口序列化 tx 时不得按行补查 Transfer 的链与币种。"""

    def _create_paid_invoice(self, project, chain, crypto, index):
        invoice = Invoice.objects.create(
            project=project,
            out_no=f"list-query-{index}",
            title="List query",
            currency_id="USD",
            amount=Decimal("10"),
            methods={},
            expires_at=timezone.now(),
        )
        transfer = Transfer.objects.create(
            chain=chain,
            block=1,
            block_hash="0x" + "aa" * 32,
            hash=f"0x{index:064x}",
            crypto=crypto,
            from_address="0x00000000000000000000000000000000000000B1",
            to_address="0x00000000000000000000000000000000000000C1",
            value=Decimal("1"),
            amount=Decimal("1"),
            status=TransferStatus.CONFIRMING,
            timestamp=int(timezone.now().timestamp()),
            datetime=timezone.now(),
        )
        Invoice.objects.filter(pk=invoice.pk).update(transfer=transfer)

    def test_query_count_does_not_grow_with_rows(self, client):
        Fiat.objects.get_or_create(code="USD")
        project = Project.objects.create(name="list-query")
        chain = make_evm_chain(code=ChainCode.Ethereum)
        crypto = Crypto.objects.create(
            name="Tether",
            symbol="USDT",
            prices={"USD": "1"},
            coingecko_id="tether-list-query",
        )
        url = f"/saas/v1/projects/{project.appid}/invoices"

        self._create_paid_invoice(project, chain, crypto, 1)
        with CaptureQueriesContext(connection) as one_row:
            resp = client.get(url, HTTP_AUTHORIZATION=AUTH)
        assert resp.status_code == 200
        assert resp.json()["results"][0]["tx"]["confirm_progress"] is not None

        for index in (2, 3, 4):
            self._create_paid_invoice(project, chain, crypto, index)
        with CaptureQueriesContext(connection) as four_rows:
            resp = client.get(url, HTTP_AUTHORIZATION=AUTH)
        assert resp.status_code == 200
        assert len(resp.json()["results"]) == 4

        assert len(four_rows.captured_queries) == len(one_row.captured_queries)
