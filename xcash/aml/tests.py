from __future__ import annotations

import time
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from aml.clients import MistTrackAmlResult
from aml.clients import MistTrackOpenApiClient
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

from chains.constants import ChainCode
from chains.models import Chain
from chains.models import ChainType
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
        self.project = Project.objects.create(name="AML Project")
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
            aml_screening_enabled=True,
            aml_screening_threshold_usd=Decimal("100"),
            aml_screening_cache_seconds=300,
            aml_screening_force_refresh_threshold_usd=Decimal("10000"),
            quicknode_misttrack_endpoint_url="https://quicknode.example",
        )

    def make_invoice(self, *, worth: Decimal = Decimal("500")) -> Invoice:
        return Invoice.objects.create(
            project=self.project,
            out_no=f"INV-{worth}",
            title="Risk invoice",
            currency="USD",
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
                "params": [{"chain": "ETH", "address": "0xabc"}],
            },
            timeout=5.0,
        )
        self.assertEqual(result.risk_level, RiskLevel.HIGH)
        self.assertEqual(result.risk_score, Decimal("88"))
        # QuickNode add-on 不返回 address_label
        self.assertIsNone(result.address_label)

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


class MistTrackOpenApiClientTests(SimpleTestCase):
    @patch("aml.clients.time.sleep", return_value=None)
    @patch("aml.clients.httpx.request")
    def test_address_risk_score_calls_v3_endpoint_with_api_key(
        self, httpx_request, _sleep
    ):
        response = httpx.Response(
            200,
            json={
                "success": True,
                "msg": "",
                "data": {
                    "risk_level": "High",
                    "score": 75,
                    "address_label": "Binance",
                },
            },
            request=httpx.Request(
                "GET", "https://openapi.misttrack.io/v3/risk_score"
            ),
        )
        httpx_request.return_value = response

        result = MistTrackOpenApiClient(api_key="secret").address_risk_score(
            coin="ETH", address="0xabc"
        )

        httpx_request.assert_called_once_with(
            "GET",
            "https://openapi.misttrack.io/v3/risk_score",
            params={"coin": "ETH", "address": "0xabc", "api_key": "secret"},
            timeout=5.0,
        )
        self.assertEqual(result.risk_level, RiskLevel.HIGH)
        self.assertEqual(result.risk_score, Decimal("75"))
        self.assertEqual(result.address_label, "Binance")
        self.assertEqual(result.raw_response["address_label"], "Binance")

    @patch("aml.clients.time.sleep", return_value=None)
    @patch("aml.clients.httpx.request")
    def test_api_error_raises_client_error(self, httpx_request, _sleep):
        httpx_request.return_value = httpx.Response(
            200,
            json={"success": False, "msg": "InvalidApiKey"},
            request=httpx.Request(
                "GET", "https://openapi.misttrack.io/v3/risk_score"
            ),
        )

        with self.assertRaisesMessage(RuntimeError, "InvalidApiKey"):
            MistTrackOpenApiClient(api_key="bad").address_risk_score(
                coin="ETH", address="0xabc"
            )

    @patch("aml.clients.time.sleep", return_value=None)
    @patch("aml.clients.httpx.request")
    def test_4xx_response_does_not_leak_api_key(self, httpx_request, _sleep):
        """HTTP 4xx 抛出的异常消息不得包含明文 api_key（防日志泄露）。"""
        httpx_request.return_value = httpx.Response(
            401,
            text="api_key=super-secret-leaked is invalid",
            request=httpx.Request(
                "GET",
                "https://openapi.misttrack.io/v3/risk_score?api_key=super-secret-leaked",
            ),
        )

        with self.assertRaises(RuntimeError) as ctx:
            MistTrackOpenApiClient(api_key="super-secret-leaked").address_risk_score(
                coin="ETH", address="0xabc"
            )

        self.assertNotIn("super-secret-leaked", str(ctx.exception))
        # 4xx 不重试
        self.assertEqual(httpx_request.call_count, 1)

    @patch("aml.clients.time.sleep", return_value=None)
    @patch("aml.clients.httpx.request")
    def test_5xx_retries_then_succeeds(self, httpx_request, _sleep):
        ok = httpx.Response(
            200,
            json={
                "success": True,
                "msg": "",
                "data": {
                    "risk_level": "Low",
                    "score": 10,
                },
            },
            request=httpx.Request(
                "GET", "https://openapi.misttrack.io/v3/risk_score"
            ),
        )
        fail = httpx.Response(
            502,
            text="Bad Gateway",
            request=httpx.Request(
                "GET", "https://openapi.misttrack.io/v3/risk_score"
            ),
        )
        httpx_request.side_effect = [fail, fail, ok]

        result = MistTrackOpenApiClient(api_key="k").address_risk_score(
            coin="ETH", address="0xabc"
        )

        self.assertEqual(result.risk_level, RiskLevel.LOW)
        self.assertEqual(httpx_request.call_count, 3)

    @patch("aml.clients.time.sleep", return_value=None)
    @patch("aml.clients.httpx.request")
    def test_429_with_retry_after_retries_then_succeeds(self, httpx_request, _sleep):
        """429 响应体携带 retry_after 时应按该值休眠后重试。"""
        ok = httpx.Response(
            200,
            json={
                "success": True,
                "msg": "",
                "data": {
                    "risk_level": "Low",
                    "score": 10,
                },
            },
            request=httpx.Request(
                "GET", "https://openapi.misttrack.io/v3/risk_score"
            ),
        )
        rate_limited = httpx.Response(
            429,
            json={"success": False, "msg": "ExceededRateLimit", "retry_after": 2},
            request=httpx.Request(
                "GET", "https://openapi.misttrack.io/v3/risk_score"
            ),
        )
        httpx_request.side_effect = [rate_limited, ok]

        result = MistTrackOpenApiClient(api_key="k").address_risk_score(
            coin="ETH", address="0xabc"
        )

        self.assertEqual(result.risk_level, RiskLevel.LOW)
        self.assertEqual(httpx_request.call_count, 2)

    @patch("aml.clients.time.sleep", return_value=None)
    @patch("aml.clients.httpx.request")
    def test_429_exhausted_retries_raises_error(self, httpx_request, _sleep):
        """429 连续重试耗尽后应抛出异常。"""
        rate_limited = httpx.Response(
            429,
            json={"success": False, "msg": "ExceededRateLimit", "retry_after": 1},
            request=httpx.Request(
                "GET", "https://openapi.misttrack.io/v3/risk_score"
            ),
        )
        httpx_request.side_effect = [rate_limited, rate_limited, rate_limited]

        with self.assertRaises(RuntimeError):
            MistTrackOpenApiClient(api_key="k").address_risk_score(
                coin="ETH", address="0xabc"
            )

        self.assertEqual(httpx_request.call_count, 3)

    @patch("aml.clients.time.sleep", return_value=None)
    @patch("aml.clients.httpx.request")
    def test_network_error_message_does_not_leak_api_key(
        self, httpx_request, _sleep
    ):
        """网络异常重试耗尽后抛出的消息不得包含 api_key。"""
        httpx_request.side_effect = httpx.ConnectError(
            "connection refused for https://openapi.misttrack.io/v3/risk_score?api_key=leak-me"
        )

        with self.assertRaises(RuntimeError) as ctx:
            MistTrackOpenApiClient(api_key="leak-me").address_risk_score(
                coin="ETH", address="0xabc"
            )

        self.assertNotIn("leak-me", str(ctx.exception))
        self.assertEqual(httpx_request.call_count, 3)


class AmlChainMappingTests(SimpleTestCase):
    def test_quicknode_maps_only_addon_supported_networks(self):
        cases = {
            ChainType.TRON: "TRX",
            1: "ETH",
            56: "BNB",
            42161: "ARBITRUM",
        }

        for chain_key, expected in cases.items():
            with self.subTest(chain_key=chain_key):
                if isinstance(chain_key, int):
                    chain = SimpleNamespace(type=ChainType.EVM, chain_id=chain_key, chain="mock")
                else:
                    chain = SimpleNamespace(type=chain_key, chain="mock")
                self.assertEqual(
                    AmlScreeningService._quicknode_misttrack_chain(chain), expected
                )

    def test_common_evm_mainnets_map_to_misttrack_openapi_coin_codes(self):
        cases = {
            (1, "ETH"): "ETH",
            (1, "USDT"): "USDT-ERC20",
            (10, "ETH"): "ETH-Optimism",
            (10, "USDT"): "USDT-Optimism",
            (10, "USDC"): "USDC-Optimism",
            (56, "BNB"): "BNB",
            (56, "USDT"): "USDT-BEP20",
            (56, "BUSD"): "BUSD-BEP20",
            (137, "POL"): "POL-Polygon",
            (137, "USDT"): "USDT-Polygon",
            (137, "USDC.E"): "USDC.e-Polygon",
            (324, "ETH"): "ETH-zkSync",
            (324, "ZK"): "ZK-zkSync",
            (4689, "IOTX"): "IOTX",
            (8453, "ETH"): "ETH-Base",
            (8453, "USDC"): "USDC-Base",
            (8453, "USDT"): "USDT-Base",
            (8453, "CBBTC"): "cbBTC-Base",
            (42161, "ETH"): "ETH-Arbitrum",
            (42161, "USDT"): "USDT-Arbitrum",
            (42161, "ARB"): "ARB-Arbitrum",
            (43114, "AVAX"): "AVAX-Avalanche",
            (43114, "USDT"): "USDT-Avalanche",
            (43114, "BTC.B"): "BTC.b-Avalanche",
        }

        for (chain_id, symbol), expected in cases.items():
            with self.subTest(chain_id=chain_id, symbol=symbol):
                chain = SimpleNamespace(type=ChainType.EVM, chain_id=chain_id, chain="mock")
                crypto = Crypto(symbol=symbol)
                self.assertEqual(
                    AmlScreeningService._misttrack_openapi_coin(
                        chain=chain, crypto=crypto
                    ),
                    expected,
                )

    def test_tron_usdt_maps_to_trc20_coin_code(self):
        chain = SimpleNamespace(type=ChainType.TRON, chain="mock")
        crypto = Crypto(symbol="USDT")

        self.assertEqual(
            AmlScreeningService._misttrack_openapi_coin(chain=chain, crypto=crypto),
            "USDT-TRC20",
        )


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
    def test_screening_disabled_does_not_create_assessment(self, score):
        self.system_settings.aml_screening_enabled = False
        self.system_settings.save(update_fields=["aml_screening_enabled"])
        invoice = self.make_invoice(worth=Decimal("500"))

        AmlScreeningService.screen_invoice(invoice.pk)

        score.assert_not_called()
        self.assertFalse(RiskAssessment.objects.filter(invoice=invoice).exists())

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
        """SaaS 模式 + 缓存命中 + enable_aml_screening=True → 正常筛查。"""
        invoice = self.make_invoice(worth=Decimal("500"))
        cache.set(
            f"saas:permission:{invoice.project.appid}",
            {"enable_aml_screening": True, "_fetched_at": time.time()},
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
        """SaaS 模式 + 缓存命中 + enable_aml_screening=False → 直接 return，不写记录。"""
        invoice = self.make_invoice(worth=Decimal("500"))
        cache.set(
            f"saas:permission:{invoice.project.appid}",
            {"enable_aml_screening": False, "_fetched_at": time.time()},
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
            {"enable_aml_screening": True, "_fetched_at": time.time()},
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
            {"enable_aml_screening": False, "_fetched_at": time.time()},
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
    @patch("aml.service.MistTrackOpenApiClient.address_risk_score")
    def test_openapi_api_key_takes_precedence_over_quicknode_endpoint(
        self, openapi_score, quicknode_score
    ):
        invoice = self.make_invoice(worth=Decimal("500"))
        self.system_settings.misttrack_openapi_api_key = "openapi-secret"
        self.system_settings.save(update_fields=["misttrack_openapi_api_key"])
        openapi_score.return_value = MistTrackAmlResult(
            risk_level=RiskLevel.HIGH,
            risk_score=Decimal("75"),
            raw_response={"risk_level": "High", "score": 75},
        )

        AmlScreeningService.screen_invoice(invoice.pk)

        openapi_score.assert_called_once_with(
            coin="ETH", address=self.transfer.from_address
        )
        quicknode_score.assert_not_called()
        assessment = RiskAssessment.objects.get(invoice=invoice)
        self.assertEqual(assessment.source, Provider.MISTTRACK_OPENAPI)
        self.assertEqual(assessment.status, RiskAssessment.Status.SUCCESS)
        invoice.refresh_from_db()
        self.assertEqual(invoice.risk_level, RiskLevel.HIGH)

    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_quicknode_unsupported_chain_does_not_create_assessment(self, score):
        invoice = self.make_invoice(worth=Decimal("500"))
        # 占位 rpc 的 active 链不能走 save()（会触发 clean 的远端校验），用 update 直接改 code。
        Chain.objects.filter(pk=self.chain.pk).update(code=ChainCode.Polygon)
        self.chain.refresh_from_db()

        AmlScreeningService.screen_invoice(invoice.pk)

        score.assert_not_called()
        self.assertFalse(RiskAssessment.objects.filter(invoice=invoice).exists())

    @patch("aml.service.MistTrackOpenApiClient.address_risk_score")
    def test_openapi_unsupported_chain_does_not_create_assessment(self, score):
        invoice = self.make_invoice(worth=Decimal("500"))
        self.system_settings.misttrack_openapi_api_key = "openapi-secret"
        self.system_settings.save(update_fields=["misttrack_openapi_api_key"])
        # 占位 rpc 的 active 链不能走 save()（会触发 clean 的远端校验），用 update 直接改 code。
        Chain.objects.filter(pk=self.chain.pk).update(code=ChainCode.Scroll)
        self.chain.refresh_from_db()

        AmlScreeningService.screen_invoice(invoice.pk)

        score.assert_not_called()
        self.assertFalse(RiskAssessment.objects.filter(invoice=invoice).exists())

    def test_provider_not_configured_does_not_create_assessment(self):
        invoice = self.make_invoice(worth=Decimal("500"))
        self.system_settings.quicknode_misttrack_endpoint_url = ""
        self.system_settings.misttrack_openapi_api_key = ""
        self.system_settings.save(
            update_fields=[
                "quicknode_misttrack_endpoint_url",
                "misttrack_openapi_api_key",
            ]
        )

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
            currency="USD",
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
        Transfer.objects.filter(pk=self.transfer.pk).update(status=TransferStatus.CONFIRMED)
        with self.captureOnCommitCallbacks(execute=True):
            InvoiceService.confirm_invoice(invoice)

        delay.assert_called_once_with(invoice.pk)


    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_invoice_success_updates_assessment_snapshot_and_cache(self, score):
        invoice = self.make_invoice(worth=Decimal("500"))
        score.return_value = MistTrackAmlResult(
            risk_level=RiskLevel.SEVERE,
            risk_score=Decimal("95"),
            raw_response={"risk_level": "Severe", "score": 95},
        )

        AmlScreeningService.screen_invoice(invoice.pk)

        score.assert_called_once()
        assessment = RiskAssessment.objects.get(invoice=invoice)
        self.assertEqual(assessment.source, Provider.QUICKNODE_MISTTRACK)
        self.assertEqual(assessment.status, RiskAssessment.Status.SUCCESS)
        self.assertEqual(assessment.risk_level, RiskLevel.SEVERE)
        self.assertEqual(assessment.risk_score, Decimal("95"))
        invoice.refresh_from_db()
        self.assertEqual(invoice.risk_level, RiskLevel.SEVERE)
        self.assertEqual(invoice.risk_score, Decimal("95"))

    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_deposit_uses_cached_address_result_without_external_query(self, score):
        deposit = self.make_deposit(worth=Decimal("500"))
        AmlScreeningService.write_cache(
            source=Provider.QUICKNODE_MISTTRACK,
            chain=self.chain.code,
            address=self.transfer.from_address,
            result={
                "risk_level": RiskLevel.MODERATE,
                "risk_score": "61",
                "raw_response": {"risk_level": "Moderate", "score": 61},
            },
            timeout=300,
        )

        AmlScreeningService.screen_deposit(deposit.pk)

        score.assert_not_called()
        assessment = RiskAssessment.objects.get(deposit=deposit)
        self.assertEqual(assessment.status, RiskAssessment.Status.SUCCESS)
        self.assertEqual(assessment.risk_level, RiskLevel.MODERATE)
        self.assertEqual(assessment.risk_score, Decimal("61"))
        deposit.refresh_from_db()
        self.assertEqual(deposit.risk_level, RiskLevel.MODERATE)
        self.assertEqual(deposit.risk_score, Decimal("61"))

    @patch("aml.service.QuicknodeMistTrackClient.address_risk_score")
    def test_force_refresh_threshold_bypasses_cache(self, score):
        deposit = self.make_deposit(worth=Decimal("10000.01"))
        score.return_value = MistTrackAmlResult(
            risk_level=RiskLevel.LOW,
            risk_score=Decimal("10"),
            raw_response={"risk_level": "Low", "score": 10},
        )
        AmlScreeningService.write_cache(
            source=Provider.QUICKNODE_MISTTRACK,
            address=self.transfer.from_address,
            result={
                "risk_level": RiskLevel.SEVERE,
                "risk_score": "99",
                "raw_response": {},
            },
            timeout=300,
        )

        AmlScreeningService.screen_deposit(deposit.pk)

        score.assert_called_once()
        deposit.refresh_from_db()
        self.assertEqual(deposit.risk_level, RiskLevel.LOW)
        self.assertEqual(deposit.risk_score, Decimal("10"))

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
