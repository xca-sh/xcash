from __future__ import annotations

import time
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import httpx
from aml.clients import MistTrackAmlResult
from aml.clients import QuicknodeMistTrackClient
from aml.models import Provider
from aml.models import RiskAssessment
from aml.models import RiskLevel
from aml.service import AmlScreeningService
from django.core.cache import cache
from django.test import SimpleTestCase
from django.test import TestCase
from django.test import override_settings
from django.utils import timezone

from chains.constants import EVM_UNKNOWN_SOURCE_ADDRESS
from chains.constants import ChainCode
from chains.models import Chain
from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import TransferType
from chains.tests_fixtures import make_evm_chain
from core.models import SystemSettings
from currencies.models import Crypto
from currencies.models import Fiat
from deposits.models import Deposit
from invoices.models import Invoice
from invoices.models import InvoiceStatus
from invoices.service import InvoiceService
from projects.models import Customer
from projects.models import Project


class AmlTestMixin:
    def setUp(self):
        cache.clear()
        Fiat.objects.get_or_create(code="USD")
        self.native = Crypto.objects.create(
            name="Ethereum",
            symbol="ETH",
            prices={"USD": "2000"},
            coingecko_id="aml-eth",
        )
        self.chain = make_evm_chain(code=ChainCode.Ethereum)
        self.project = Project.objects.create(
            name="AML Project",
            aml_screening_enabled=True,
            aml_screening_threshold_usd=Decimal("100"),
        )
        self.customer = Customer.objects.create(project=self.project, uid="u-1")
        self.transfer = Transfer.objects.create(
            chain=self.chain,
            block=100,
            block_hash="0x" + "ab" * 32,
            hash="0x" + "cd" * 32,
            crypto=self.native,
            from_address="0x1111111111111111111111111111111111111111",
            to_address="0x2222222222222222222222222222222222222222",
            value=10**18,
            amount=Decimal("1"),
            type=TransferType.Invoice,
            timestamp=1_700_000_000,
            datetime=timezone.now(),
        )
        self.system_settings = SystemSettings.objects.create(
            quicknode_misttrack_endpoint_url="https://quicknode.example",
        )

    def make_invoice(self, *, worth: Decimal = Decimal("500")) -> Invoice:
        return Invoice.objects.create(
            project=self.project,
            out_no=f"INV-{worth}",
            title="Risk invoice",
            currency_id="USD",
            amount=worth,
            methods={"ETH": ["ethereum-mainnet"]},
            crypto=self.native,
            chain=self.chain,
            pay_amount=Decimal("1"),
            pay_address=self.transfer.to_address,
            worth=worth,
            transfer=self.transfer,
            status=InvoiceStatus.COMPLETED,
            expires_at=timezone.now() + timedelta(minutes=10),
        )

    def make_deposit(self, *, worth: Decimal = Decimal("50")) -> Deposit:
        self.transfer.type = TransferType.Deposit
        self.transfer.save(update_fields=["type"])
        return Deposit.objects.create(
            customer=self.customer,
            transfer=self.transfer,
            worth=worth,
        )


class QuicknodeMistTrackClientTests(SimpleTestCase):
    @patch("aml.clients.time.sleep", return_value=None)
    @patch("aml.clients.httpx.request")
    def test_address_risk_score_posts_json_rpc_payload(self, httpx_request, _sleep):
        response = httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "risk_level": "High",
                    "score": 88,
                },
            },
            request=httpx.Request("POST", "https://quicknode.example"),
        )
        httpx_request.return_value = response

        result = QuicknodeMistTrackClient(
            endpoint_url="https://quicknode.example"
        ).address_risk_score(chain="ETH", address="0xabc")

        httpx_request.assert_called_once_with(
            "POST",
            "https://quicknode.example",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "mt_addressRiskScore",
                "params": ["ETH", "0xabc"],
            },
            timeout=5.0,
        )
        self.assertEqual(result.risk_level, RiskLevel.HIGH)
        self.assertEqual(result.risk_score, Decimal("88"))

    @patch("aml.clients.time.sleep", return_value=None)
    @patch("aml.clients.httpx.request")
    def test_json_rpc_error_raises_client_error(self, httpx_request, _sleep):
        httpx_request.return_value = httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "error": {"message": "bad request"}},
            request=httpx.Request("POST", "https://quicknode.example"),
        )

        with self.assertRaisesMessage(RuntimeError, "bad request"):
            QuicknodeMistTrackClient(
                endpoint_url="https://quicknode.example"
            ).address_risk_score(chain="ETH", address="0xabc")

    @patch("aml.clients.time.sleep", return_value=None)
    @patch("aml.clients.httpx.request")
    def test_4xx_raises_without_retry(self, httpx_request, _sleep):
        httpx_request.return_value = httpx.Response(
            401,
            text="unauthorized",
            request=httpx.Request("POST", "https://quicknode.example"),
        )

        with self.assertRaises(RuntimeError):
            QuicknodeMistTrackClient(
                endpoint_url="https://quicknode.example"
            ).address_risk_score(chain="ETH", address="0xabc")

        self.assertEqual(httpx_request.call_count, 1)

    @patch("aml.clients.time.sleep", return_value=None)
    @patch("aml.clients.httpx.request")
    def test_5xx_and_429_retry_then_succeed(self, httpx_request, _sleep):
        ok = httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"risk_level": "Low", "score": 10},
            },
            request=httpx.Request("POST", "https://quicknode.example"),
        )
        bad_gateway = httpx.Response(
            502,
            text="Bad Gateway",
            request=httpx.Request("POST", "https://quicknode.example"),
        )
        rate_limited = httpx.Response(
            429,
            text="Too Many Requests",
            request=httpx.Request("POST", "https://quicknode.example"),
        )
        httpx_request.side_effect = [bad_gateway, rate_limited, ok]

        result = QuicknodeMistTrackClient(
            endpoint_url="https://quicknode.example"
        ).address_risk_score(chain="ETH", address="0xabc")

        self.assertEqual(result.risk_level, RiskLevel.LOW)
        self.assertEqual(httpx_request.call_count, 3)

    @patch("aml.clients.time.sleep", return_value=None)
    @patch("aml.clients.httpx.request")
    def test_exhausted_retries_raise_without_leaking_endpoint_token(
        self, httpx_request, _sleep
    ):
        httpx_request.side_effect = httpx.ConnectError("connection refused")

        with self.assertRaises(RuntimeError) as ctx:
            QuicknodeMistTrackClient(
                endpoint_url="https://demo.quiknode.pro/secret-token/"
            ).address_risk_score(chain="ETH", address="0xabc")

        self.assertNotIn("secret-token", str(ctx.exception))
        self.assertEqual(httpx_request.call_count, 3)


@override_settings(IS_SAAS=False)
class AmlScreeningServiceTests(AmlTestMixin, TestCase):
    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_invoice_below_threshold_does_not_create_assessment(self, score):
        invoice = self.make_invoice(worth=Decimal("99.99"))

        AmlScreeningService.screen_invoice(invoice.pk)

        score.assert_not_called()
        self.assertFalse(RiskAssessment.objects.filter(invoice=invoice).exists())
        invoice.refresh_from_db()
        self.assertIsNone(invoice.risk_level)
        self.assertIsNone(invoice.risk_score)

    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_deposit_below_threshold_does_not_create_assessment(self, score):
        deposit = self.make_deposit(worth=Decimal("99.99"))

        AmlScreeningService.screen_deposit(deposit.pk)

        score.assert_not_called()
        self.assertFalse(RiskAssessment.objects.filter(deposit=deposit).exists())
        deposit.refresh_from_db()
        self.assertIsNone(deposit.risk_level)
        self.assertIsNone(deposit.risk_score)

    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_deposit_unknown_source_address_does_not_create_assessment(self, score):
        deposit = self.make_deposit(worth=Decimal("500"))
        Transfer.objects.filter(pk=self.transfer.pk).update(
            from_address=EVM_UNKNOWN_SOURCE_ADDRESS
        )

        AmlScreeningService.screen_deposit(deposit.pk)

        score.assert_not_called()
        self.assertFalse(RiskAssessment.objects.filter(deposit=deposit).exists())
        deposit.refresh_from_db()
        self.assertIsNone(deposit.risk_level)
        self.assertIsNone(deposit.risk_score)

    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_screening_disabled_does_not_create_assessment(self, score):
        self.project.aml_screening_enabled = False
        self.project.save(update_fields=["aml_screening_enabled"])
        invoice = self.make_invoice(worth=Decimal("500"))

        AmlScreeningService.screen_invoice(invoice.pk)

        score.assert_not_called()
        self.assertFalse(RiskAssessment.objects.filter(invoice=invoice).exists())

    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_screening_follows_each_project_own_settings(self, score):
        """同金额收款按各自所属项目的开关与阈值判定，项目之间互不影响。"""
        score.return_value = MistTrackAmlResult(
            risk_level=RiskLevel.LOW,
            risk_score=Decimal("10"),
            raw_response={},
        )
        strict_project = Project.objects.create(
            name="High Threshold Project",
            aml_screening_enabled=True,
            aml_screening_threshold_usd=Decimal("1000"),
        )
        strict_customer = Customer.objects.create(project=strict_project, uid="u-2")
        invoice = self.make_invoice(worth=Decimal("500"))
        self.transfer.type = TransferType.Deposit
        self.transfer.save(update_fields=["type"])
        deposit = Deposit.objects.create(
            customer=strict_customer,
            transfer=self.transfer,
            worth=Decimal("500"),
        )

        AmlScreeningService.screen_invoice(invoice.pk)
        AmlScreeningService.screen_deposit(deposit.pk)

        score.assert_called_once()
        self.assertTrue(RiskAssessment.objects.filter(invoice=invoice).exists())
        self.assertFalse(RiskAssessment.objects.filter(deposit=deposit).exists())

    # ===== SaaS gate（spec §5） =====
    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_invoice_self_hosted_mode_screens_normally(self, score):
        """自托管模式（class-level IS_SAAS=False），gate 直接放行。"""
        invoice = self.make_invoice(worth=Decimal("500"))
        score.return_value = MistTrackAmlResult(
            risk_level=RiskLevel.SEVERE,
            risk_score=Decimal("95"),
            raw_response={},
        )

        AmlScreeningService.screen_invoice(invoice.pk)

        score.assert_called_once()
        assessment = RiskAssessment.objects.get(invoice=invoice)
        self.assertEqual(assessment.status, RiskAssessment.Status.SUCCESS)

    @override_settings(IS_SAAS=True, SAAS_API_TOKEN="t")
    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_invoice_saas_permission_granted_screens(self, score):
        """SaaS 模式 + 缓存命中 + enable_risk_marking=True → 正常筛查。"""
        invoice = self.make_invoice(worth=Decimal("500"))
        cache.set(
            f"saas:permission:{invoice.project.appid}",
            {"enable_risk_marking": True, "_fetched_at": time.time()},
            None,
        )
        score.return_value = MistTrackAmlResult(
            risk_level=RiskLevel.SEVERE,
            risk_score=Decimal("95"),
            raw_response={},
        )

        AmlScreeningService.screen_invoice(invoice.pk)

        score.assert_called_once()
        assessment = RiskAssessment.objects.get(invoice=invoice)
        self.assertEqual(assessment.status, RiskAssessment.Status.SUCCESS)

    @override_settings(IS_SAAS=True, SAAS_API_TOKEN="t")
    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_invoice_saas_permission_denied_does_not_create_assessment(self, score):
        """SaaS 模式 + 缓存命中 + enable_risk_marking=False → 直接 return，不写记录。"""
        invoice = self.make_invoice(worth=Decimal("500"))
        cache.set(
            f"saas:permission:{invoice.project.appid}",
            {"enable_risk_marking": False, "_fetched_at": time.time()},
            None,
        )

        AmlScreeningService.screen_invoice(invoice.pk)

        score.assert_not_called()
        self.assertFalse(RiskAssessment.objects.filter(invoice=invoice).exists())
        invoice.refresh_from_db()
        self.assertIsNone(invoice.risk_level)

    @override_settings(IS_SAAS=True, SAAS_API_TOKEN="t")
    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_invoice_saas_cold_cache_fails_closed(self, score):
        """SaaS 模式 + 冷缓存 → fail-closed → 直接 return，不调 MistTrack 也不写记录。"""
        invoice = self.make_invoice(worth=Decimal("500"))
        # 不预写缓存，cache.clear() 已在 setUp 跑过

        AmlScreeningService.screen_invoice(invoice.pk)

        score.assert_not_called()
        self.assertFalse(RiskAssessment.objects.filter(invoice=invoice).exists())

    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_deposit_self_hosted_mode_screens_normally(self, score):
        """自托管模式（class-level IS_SAAS=False），gate 直接放行。"""
        deposit = self.make_deposit(worth=Decimal("500"))
        score.return_value = MistTrackAmlResult(
            risk_level=RiskLevel.SEVERE,
            risk_score=Decimal("95"),
            raw_response={},
        )

        AmlScreeningService.screen_deposit(deposit.pk)

        score.assert_called_once()
        assessment = RiskAssessment.objects.get(deposit=deposit)
        self.assertEqual(assessment.status, RiskAssessment.Status.SUCCESS)

    @override_settings(IS_SAAS=True, SAAS_API_TOKEN="t")
    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_deposit_saas_permission_granted_screens(self, score):
        deposit = self.make_deposit(worth=Decimal("500"))
        cache.set(
            f"saas:permission:{deposit.customer.project.appid}",
            {"enable_risk_marking": True, "_fetched_at": time.time()},
            None,
        )
        score.return_value = MistTrackAmlResult(
            risk_level=RiskLevel.SEVERE,
            risk_score=Decimal("95"),
            raw_response={},
        )

        AmlScreeningService.screen_deposit(deposit.pk)

        score.assert_called_once()
        assessment = RiskAssessment.objects.get(deposit=deposit)
        self.assertEqual(assessment.status, RiskAssessment.Status.SUCCESS)

    @override_settings(IS_SAAS=True, SAAS_API_TOKEN="t")
    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_deposit_saas_permission_denied_does_not_create_assessment(self, score):
        deposit = self.make_deposit(worth=Decimal("500"))
        cache.set(
            f"saas:permission:{deposit.customer.project.appid}",
            {"enable_risk_marking": False, "_fetched_at": time.time()},
            None,
        )

        AmlScreeningService.screen_deposit(deposit.pk)

        score.assert_not_called()
        self.assertFalse(RiskAssessment.objects.filter(deposit=deposit).exists())

    @override_settings(IS_SAAS=True, SAAS_API_TOKEN="t")
    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_deposit_saas_cold_cache_fails_closed(self, score):
        deposit = self.make_deposit(worth=Decimal("500"))

        AmlScreeningService.screen_deposit(deposit.pk)

        score.assert_not_called()
        self.assertFalse(RiskAssessment.objects.filter(deposit=deposit).exists())

    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_unsupported_chain_does_not_create_assessment(self, score):
        """MistTrack 只覆盖主网：测试网（含 Nile）与未覆盖的主网都不查询、不写记录。"""
        invoice = self.make_invoice(worth=Decimal("500"))
        for code in (ChainCode.Sepolia, ChainCode.Nile, ChainCode.Linea):
            with self.subTest(code=code):
                # 占位 rpc 的 active 链不能走 save()（会触发 clean 的远端校验），用 update 直接改 code。
                Chain.objects.filter(pk=self.chain.pk).update(code=code)

                AmlScreeningService.screen_invoice(invoice.pk)

                score.assert_not_called()
                self.assertFalse(
                    RiskAssessment.objects.filter(invoice=invoice).exists()
                )

    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_tron_mainnet_queries_with_tron_blockchain_value(self, score):
        invoice = self.make_invoice(worth=Decimal("500"))
        Chain.objects.filter(pk=self.chain.pk).update(code=ChainCode.Tron)
        score.return_value = MistTrackAmlResult(
            risk_level=RiskLevel.LOW,
            risk_score=Decimal("10"),
            raw_response={},
        )

        AmlScreeningService.screen_invoice(invoice.pk)

        score.assert_called_once_with(chain="TRON", address=self.transfer.from_address)

    def test_provider_not_configured_does_not_create_assessment(self):
        invoice = self.make_invoice(worth=Decimal("500"))
        self.system_settings.quicknode_misttrack_endpoint_url = ""
        self.system_settings.save(update_fields=["quicknode_misttrack_endpoint_url"])

        AmlScreeningService.screen_invoice(invoice.pk)

        self.assertFalse(RiskAssessment.objects.filter(invoice=invoice).exists())


@override_settings(IS_SAAS=False)
class AmlBusinessDispatchTests(AmlTestMixin, TestCase):
    @patch("aml.tasks.screen_invoice_aml.delay")
    @patch("invoices.service.send_saas_callback")
    @patch("invoices.service.WebhookService.create_event")
    def test_invoice_confirm_enqueues_aml_after_transaction_commit(
        self, _create_event, _callback, delay
    ):
        invoice = Invoice.objects.create(
            project=self.project,
            out_no="aml-match",
            title="AML match",
            currency_id="USD",
            amount=Decimal("500"),
            methods={"ETH": ["ethereum-mainnet"]},
            worth=Decimal("500"),
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        Invoice.objects.filter(pk=invoice.pk).update(
            crypto=self.native,
            chain=self.chain,
            pay_address=self.transfer.to_address,
            pay_amount=self.transfer.amount,
        )
        invoice.refresh_from_db()
        self.transfer.datetime = timezone.now()
        self.transfer.save(update_fields=["datetime"])

        matched = InvoiceService.try_match_invoice(self.transfer)
        self.assertTrue(matched)
        invoice.refresh_from_db()

        delay.assert_not_called()
        Transfer.objects.filter(pk=self.transfer.pk).update(
            status=TransferStatus.CONFIRMED
        )
        with self.captureOnCommitCallbacks(execute=True):
            InvoiceService.confirm_invoice(invoice)

        delay.assert_called_once_with(invoice.pk)

    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_invoice_success_updates_assessment_and_snapshot(self, score):
        invoice = self.make_invoice(worth=Decimal("500"))
        score.return_value = MistTrackAmlResult(
            risk_level=RiskLevel.SEVERE,
            risk_score=Decimal("95"),
            raw_response={"risk_level": "Severe", "score": 95},
        )

        AmlScreeningService.screen_invoice(invoice.pk)

        score.assert_called_once_with(chain="ETH", address=self.transfer.from_address)
        assessment = RiskAssessment.objects.get(invoice=invoice)
        self.assertEqual(assessment.source, Provider.QUICKNODE_MISTTRACK)
        self.assertEqual(assessment.status, RiskAssessment.Status.SUCCESS)
        self.assertEqual(assessment.risk_level, RiskLevel.SEVERE)
        self.assertEqual(assessment.risk_score, Decimal("95"))
        invoice.refresh_from_db()
        self.assertEqual(invoice.risk_level, RiskLevel.SEVERE)
        self.assertEqual(invoice.risk_score, Decimal("95"))

    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_same_address_is_requeried_for_every_payment(self, score):
        """同一付款地址的每笔收款都实时查询，不复用前一笔的结果。

        防止攻击者先用干净资金付款让地址被判低风险，再转入黑币付款沿用旧结论。
        """
        score.side_effect = [
            MistTrackAmlResult(
                risk_level=RiskLevel.LOW,
                risk_score=Decimal("10"),
                raw_response={},
            ),
            MistTrackAmlResult(
                risk_level=RiskLevel.SEVERE,
                risk_score=Decimal("99"),
                raw_response={},
            ),
        ]
        invoice = self.make_invoice(worth=Decimal("500"))
        AmlScreeningService.screen_invoice(invoice.pk)

        deposit = self.make_deposit(worth=Decimal("500"))
        AmlScreeningService.screen_deposit(deposit.pk)

        self.assertEqual(score.call_count, 2)
        invoice.refresh_from_db()
        deposit.refresh_from_db()
        self.assertEqual(invoice.risk_level, RiskLevel.LOW)
        self.assertEqual(deposit.risk_level, RiskLevel.SEVERE)
        self.assertEqual(deposit.risk_score, Decimal("99"))

    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_external_failure_records_failed_and_clears_snapshot(self, score):
        invoice = self.make_invoice(worth=Decimal("500"))
        invoice.risk_level = RiskLevel.HIGH
        invoice.risk_score = Decimal("80")
        invoice.save(update_fields=["risk_level", "risk_score", "updated_at"])
        score.side_effect = RuntimeError("quicknode down")

        AmlScreeningService.screen_invoice(invoice.pk)

        assessment = RiskAssessment.objects.get(invoice=invoice)
        self.assertEqual(assessment.status, RiskAssessment.Status.FAILED)
        self.assertIn("quicknode down", assessment.error_message)
        invoice.refresh_from_db()
        self.assertIsNone(invoice.risk_level)
        self.assertIsNone(invoice.risk_score)
