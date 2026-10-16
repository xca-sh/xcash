from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import httpx
from aml.clients import MistTrackAmlResult
from aml.clients import QuicknodeMistTrackClient
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
from chains.models import ConfirmMode
from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import TransferType
from chains.models import VaultSlot
from chains.models import VaultSlotUsage
from chains.tests_fixtures import make_evm_chain
from common.saas_callback import CallbackEvent
from core.models import SystemSettings
from currencies.models import Crypto
from currencies.models import Fiat
from deposits.models import Deposit
from deposits.service import DepositService
from invoices.models import Invoice
from invoices.models import InvoiceStatus
from invoices.service import InvoiceService
from projects.models import Customer
from projects.models import Project
from webhooks.models import WebhookEvent


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


def low_risk() -> MistTrackAmlResult:
    return MistTrackAmlResult(
        risk_level=RiskLevel.LOW, risk_score=Decimal("10"), raw_response={}
    )


def high_risk() -> MistTrackAmlResult:
    return MistTrackAmlResult(
        risk_level=RiskLevel.HIGH,
        risk_score=Decimal("88"),
        raw_response={"risk_level": "High", "score": 88},
    )


class AmlShouldScreenTests(AmlTestMixin, TestCase):
    def test_requires_switch_threshold_covered_chain_known_source_and_endpoint(self):
        self.assertTrue(
            AmlScreeningService.should_screen(
                project=self.project, worth=Decimal("100.01"), transfer=self.transfer
            )
        )
        # 价值必须严格大于阈值
        self.assertFalse(
            AmlScreeningService.should_screen(
                project=self.project, worth=Decimal("100"), transfer=self.transfer
            )
        )

        self.project.aml_screening_enabled = False
        self.assertFalse(
            AmlScreeningService.should_screen(
                project=self.project, worth=Decimal("500"), transfer=self.transfer
            )
        )
        self.project.aml_screening_enabled = True

        # MistTrack 只覆盖主网：测试网（含 Nile）与未覆盖的主网不查询
        for code in (ChainCode.Sepolia, ChainCode.Nile, ChainCode.Linea):
            with self.subTest(code=code):
                self.transfer.chain.code = code
                self.assertFalse(
                    AmlScreeningService.should_screen(
                        project=self.project,
                        worth=Decimal("500"),
                        transfer=self.transfer,
                    )
                )
        self.transfer.chain.code = ChainCode.Ethereum

        self.transfer.from_address = EVM_UNKNOWN_SOURCE_ADDRESS
        self.assertFalse(
            AmlScreeningService.should_screen(
                project=self.project, worth=Decimal("500"), transfer=self.transfer
            )
        )
        self.transfer.from_address = "0x1111111111111111111111111111111111111111"

        self.system_settings.quicknode_misttrack_endpoint_url = ""
        # 系统参数缓存在事务提交后失效
        with self.captureOnCommitCallbacks(execute=True):
            self.system_settings.save(
                update_fields=["quicknode_misttrack_endpoint_url"]
            )
        self.assertFalse(
            AmlScreeningService.should_screen(
                project=self.project, worth=Decimal("500"), transfer=self.transfer
            )
        )


class AmlScreenTransferTests(AmlTestMixin, TestCase):
    @patch("aml.service.send_saas_callback")
    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_result_is_kept_on_transfer_until_payment_confirms(self, score, report):
        score.return_value = high_risk()
        invoice = self.make_invoice(worth=Decimal("500"))
        Invoice.objects.filter(pk=invoice.pk).update(status=InvoiceStatus.WAITING)

        AmlScreeningService.screen_transfer(self.transfer.pk)

        score.assert_called_once_with(chain="ETH", address=self.transfer.from_address)
        assessment = RiskAssessment.objects.get(transfer=self.transfer)
        self.assertEqual(assessment.status, RiskAssessment.Status.SUCCESS)
        self.assertEqual(assessment.risk_level, RiskLevel.HIGH)
        self.assertEqual(assessment.risk_score, Decimal("88"))
        # 收款尚未确认：不写业务快照、不计费
        invoice.refresh_from_db()
        self.assertIsNone(invoice.risk_level)
        report.assert_not_called()

    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_provider_failure_records_failed_assessment(self, score):
        score.side_effect = RuntimeError("quicknode down")

        AmlScreeningService.screen_transfer(self.transfer.pk)

        assessment = RiskAssessment.objects.get(transfer=self.transfer)
        self.assertEqual(assessment.status, RiskAssessment.Status.FAILED)
        self.assertIn("quicknode down", assessment.error_message)
        self.assertIsNone(assessment.risk_level)

    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_dropped_transfer_is_not_queried(self, score):
        missing_pk = self.transfer.pk
        self.transfer.delete()

        AmlScreeningService.screen_transfer(missing_pk)

        score.assert_not_called()

    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_tron_mainnet_queries_with_tron_blockchain_value(self, score):
        score.return_value = low_risk()
        Chain.objects.filter(pk=self.chain.pk).update(code=ChainCode.Tron)

        AmlScreeningService.screen_transfer(self.transfer.pk)

        score.assert_called_once_with(chain="TRON", address=self.transfer.from_address)

    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_same_address_is_requeried_for_every_transfer(self, score):
        """同一付款地址的每笔转账都实时查询，不复用前一笔的结果。

        防止攻击者先用干净资金付款让地址被判低风险，再转入黑币付款沿用旧结论。
        """
        score.side_effect = [low_risk(), high_risk()]
        second = Transfer.objects.create(
            chain=self.chain,
            block=101,
            block_hash="0x" + "ef" * 32,
            hash="0x" + "ee" * 32,
            crypto=self.native,
            from_address=self.transfer.from_address,
            to_address=self.transfer.to_address,
            value=10**18,
            amount=Decimal("1"),
            type=TransferType.Deposit,
            timestamp=1_700_000_100,
            datetime=timezone.now(),
        )

        AmlScreeningService.screen_transfer(self.transfer.pk)
        AmlScreeningService.screen_transfer(second.pk)

        self.assertEqual(score.call_count, 2)
        self.assertEqual(
            RiskAssessment.objects.get(transfer=second).risk_level, RiskLevel.HIGH
        )

    @patch("aml.service.send_saas_callback")
    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_late_result_is_applied_to_confirmed_invoice_and_billed(
        self, score, report
    ):
        score.return_value = high_risk()
        invoice = self.make_invoice(worth=Decimal("500"))
        Transfer.objects.filter(pk=self.transfer.pk).update(
            status=TransferStatus.CONFIRMED
        )

        AmlScreeningService.screen_transfer(self.transfer.pk)

        invoice.refresh_from_db()
        self.assertEqual(invoice.risk_level, RiskLevel.HIGH)
        self.assertEqual(invoice.risk_score, Decimal("88"))
        callback = report.call_args.args[0]
        self.assertEqual(
            (callback.event, callback.appid, callback.sys_no),
            (
                CallbackEvent.AML_SCREENED,
                self.project.appid,
                f"aml-screening:{invoice.sys_no}",
            ),
        )

    @patch("aml.service.send_saas_callback")
    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_late_result_is_applied_to_confirmed_deposit(self, score, report):
        score.return_value = high_risk()
        deposit = self.make_deposit(worth=Decimal("500"))
        Transfer.objects.filter(pk=self.transfer.pk).update(
            status=TransferStatus.CONFIRMED
        )

        AmlScreeningService.screen_transfer(self.transfer.pk)

        deposit.refresh_from_db()
        self.assertEqual(deposit.risk_level, RiskLevel.HIGH)
        self.assertEqual(
            report.call_args.args[0].sys_no, f"aml-screening:{deposit.sys_no}"
        )

    @patch("aml.service.send_saas_callback")
    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_failed_query_is_not_billed(self, score, report):
        score.side_effect = RuntimeError("quicknode down")
        self.make_invoice(worth=Decimal("500"))
        Transfer.objects.filter(pk=self.transfer.pk).update(
            status=TransferStatus.CONFIRMED
        )

        AmlScreeningService.screen_transfer(self.transfer.pk)

        report.assert_not_called()


@override_settings(IS_SAAS=False)
class AmlPaymentFlowTests(AmlTestMixin, TestCase):
    def make_waiting_invoice(self) -> Invoice:
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
        return invoice

    @patch("invoices.service.screen_transfer_aml.delay")
    def test_invoice_match_starts_screening(self, delay):
        self.make_waiting_invoice()

        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(InvoiceService.try_match_invoice(self.transfer))

        delay.assert_called_once_with(self.transfer.pk)
        self.transfer.refresh_from_db()
        self.assertEqual(self.transfer.confirm_mode, ConfirmMode.FULL)

    @patch("invoices.service.screen_transfer_aml.delay")
    def test_quick_confirm_invoice_is_not_screened(self, delay):
        """快速确认优先：价值高于 AML 阈值但低于快速确认阈值的账单照常快速确认、不查 AML。"""
        self.project.fast_confirm_threshold = Decimal("1000")
        self.project.save(update_fields=["fast_confirm_threshold"])
        self.make_waiting_invoice()

        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(InvoiceService.try_match_invoice(self.transfer))

        delay.assert_not_called()
        self.transfer.refresh_from_db()
        self.assertEqual(self.transfer.confirm_mode, ConfirmMode.QUICK)

    @patch("deposits.service.screen_transfer_aml.delay")
    def test_deposit_match_starts_screening(self, delay):
        Transfer.objects.filter(pk=self.transfer.pk).update(
            type=TransferType.Unmatched, amount=Decimal("1")
        )
        self.transfer.refresh_from_db()
        VaultSlot.objects.create(
            customer=self.customer,
            chain=self.chain,
            usage=VaultSlotUsage.DEPOSIT,
            address=self.transfer.to_address,
            salt=b"\x33" * 32,
        )

        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(DepositService.try_match_deposit_transfer(self.transfer))

        # 1 ETH × 2000 USD 超过 100 USD 阈值
        delay.assert_called_once_with(self.transfer.pk)

    @patch("aml.service.send_saas_callback")
    def test_invoice_confirm_writes_risk_into_snapshot_webhook_and_billing(
        self, report
    ):
        invoice = self.make_waiting_invoice()
        Invoice.objects.filter(pk=invoice.pk).update(transfer=self.transfer)
        RiskAssessment.objects.create(
            transfer=self.transfer,
            status=RiskAssessment.Status.SUCCESS,
            address=self.transfer.from_address,
            risk_level=RiskLevel.HIGH,
            risk_score=Decimal("88"),
        )
        Transfer.objects.filter(pk=self.transfer.pk).update(
            status=TransferStatus.CONFIRMED
        )

        InvoiceService.confirm_invoice(invoice)

        invoice.refresh_from_db()
        self.assertEqual(invoice.risk_level, RiskLevel.HIGH)
        event = WebhookEvent.objects.get(project=self.project)
        self.assertEqual(event.payload["data"]["risk_level"], RiskLevel.HIGH)
        self.assertEqual(event.payload["data"]["risk_score"], "88")
        self.assertEqual(
            report.call_args.args[0].sys_no, f"aml-screening:{invoice.sys_no}"
        )

    @patch("aml.service.send_saas_callback")
    def test_deposit_confirm_writes_risk_into_snapshot_and_webhook(self, report):
        deposit = self.make_deposit(worth=Decimal("500"))
        RiskAssessment.objects.create(
            transfer=self.transfer,
            status=RiskAssessment.Status.SUCCESS,
            address=self.transfer.from_address,
            risk_level=RiskLevel.SEVERE,
            risk_score=Decimal("95"),
        )
        Transfer.objects.filter(pk=self.transfer.pk).update(
            status=TransferStatus.CONFIRMED
        )
        self.transfer.refresh_from_db()
        deposit.refresh_from_db()

        DepositService.confirm_deposit(deposit)

        deposit.refresh_from_db()
        self.assertEqual(deposit.risk_level, RiskLevel.SEVERE)
        event = WebhookEvent.objects.get(project=self.project)
        self.assertEqual(event.payload["data"]["risk_level"], RiskLevel.SEVERE)
        report.assert_called_once()

    @patch("aml.service.send_saas_callback")
    def test_confirm_before_result_sends_webhook_without_risk(self, report):
        """查询赶不上确认：webhook 照常发出、不暂缓，风险字段为空，也不计费。"""
        invoice = self.make_waiting_invoice()
        Invoice.objects.filter(pk=invoice.pk).update(transfer=self.transfer)
        Transfer.objects.filter(pk=self.transfer.pk).update(
            status=TransferStatus.CONFIRMED
        )

        InvoiceService.confirm_invoice(invoice)

        event = WebhookEvent.objects.get(project=self.project)
        self.assertIsNone(event.payload["data"]["risk_level"])
        report.assert_not_called()
