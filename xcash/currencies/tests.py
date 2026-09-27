from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock
from unittest.mock import patch

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory
from web3 import Web3

from chains.capabilities import ChainProductCapabilityService
from chains.constants import ChainCode
from chains.models import Chain
from chains.tests_fixtures import make_evm_chain
from core.models import SystemSettings
from currencies.models import Crypto
from currencies.models import CryptoOnChain
from currencies.models import Fiat
from currencies.models import PriceUnavailableError
from currencies.tasks import refresh_crypto_prices
from currencies.views import MetadataView


class CustomTokenPricingTests(TestCase):
    """未上 CoinGecko 的自定义代币：不进支付、价格优雅降级。"""

    def setUp(self):
        self.chain = Chain.objects.create(code=ChainCode.Ethereum, rpc="", active=False)
        # 无 coingecko_id 的自定义代币
        self.custom = Crypto.objects.create(name="ProjectCoin", symbol="PJC")
        # 有行情源的币（稳定币锚定 USD）
        self.usdt = Crypto.objects.create(
            name="Tether", symbol="USDT", coingecko_id="tether"
        )
        CryptoOnChain.objects.create(
            crypto=self.usdt,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000e01"
            ),
            decimals=6,
        )

    def test_blank_coingecko_id_normalized_to_null(self):
        # 空 coingecko_id 落库归一为 NULL，多条无 slug 币才能并存而不撞唯一约束。
        self.assertIsNone(self.custom.coingecko_id)
        other = Crypto.objects.create(name="OtherCoin", symbol="OTC")
        self.assertIsNone(other.coingecko_id)

    def test_price_without_source_raises_price_unavailable(self):
        # 无价格源的币取价抛明确领域异常，而非裸 KeyError。
        with self.assertRaises(PriceUnavailableError):
            self.custom.price("USD")

    def test_usd_amount_degrades_to_zero_without_price(self):
        # 非支付资产流转用的 usd_amount 在缺价时降级为 0，不阻断业务。
        self.assertEqual(self.custom.usd_amount(Decimal("100")), Decimal("0"))

    def test_is_payable_reflects_price_source(self):
        self.assertFalse(self.custom.is_payable())  # 无 slug、非稳定币
        self.assertTrue(self.usdt.is_payable())  # USD 锚定稳定币

    def test_custom_token_excluded_from_invoice_methods(self):
        # 核心业务规则：无价代币不作为支付方式，但有价的币正常可用。
        self.assertFalse(
            ChainProductCapabilityService.supports_existing_invoice_method(
                chain=self.chain, crypto=self.custom
            )
        )
        self.assertTrue(
            ChainProductCapabilityService.supports_existing_invoice_method(
                chain=self.chain, crypto=self.usdt
            )
        )

    def test_mainstream_crypto_icons_are_available(self):
        for symbol in (
            "ETH",
            "BNB",
            "TRX",
            "USDT",
            "USDC",
            "DAI",
            "WETH",
            "WBTC",
            "CBBTC",
            "LINK",
            "UNI",
            "AAVE",
            "ARB",
            "OP",
            "USDC.E",
        ):
            with self.subTest(symbol=symbol):
                crypto = Crypto(symbol=symbol)
                self.assertTrue(crypto.icon.startswith("https://"))

    def test_unknown_crypto_icon_is_empty(self):
        crypto = Crypto(symbol="PROJECT")
        self.assertEqual(crypto.icon, "")

    def test_tron_invoice_allows_usdt_and_native_trx_only(self):
        # Tron 账单收款放行 USDT 与原生 TRX；其余有价 TRC20 仍不作为支付方式。
        tron = Chain.objects.create(
            code=ChainCode.Tron, rpc="", tron_api_key="", active=False
        )
        other_trc20 = Crypto.objects.create(
            name="OtherTrc20", symbol="OTC", coingecko_id="other-trc20"
        )
        CryptoOnChain.objects.create(
            crypto=self.usdt,
            chain=tron,
            address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            decimals=6,
        )
        CryptoOnChain.objects.create(
            crypto=other_trc20,
            chain=tron,
            address="TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj",
            decimals=6,
        )
        self.assertTrue(
            ChainProductCapabilityService.supports_existing_invoice_method(
                chain=tron, crypto=tron.native_coin
            )
        )
        self.assertTrue(
            ChainProductCapabilityService.supports_existing_invoice_method(
                chain=tron, crypto=self.usdt
            )
        )
        self.assertFalse(
            ChainProductCapabilityService.supports_existing_invoice_method(
                chain=tron, crypto=other_trc20
            )
        )

    def test_tron_deposit_address_allows_usdt_and_native_trx_only(self):
        # Tron VaultSlot 充币地址放行 USDT 与原生 TRX；其余 TRC20 暂不开放。
        tron = Chain.objects.create(
            code=ChainCode.Tron, rpc="", tron_api_key="", active=False
        )
        other_trc20 = Crypto.objects.create(
            name="DepositOtherTrc20", symbol="DOT", coingecko_id="deposit-other-trc20"
        )
        CryptoOnChain.objects.create(
            crypto=self.usdt,
            chain=tron,
            address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            decimals=6,
        )
        CryptoOnChain.objects.create(
            crypto=other_trc20,
            chain=tron,
            address="TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj",
            decimals=6,
        )

        self.assertTrue(
            ChainProductCapabilityService.supports_deposit_address(
                chain=tron, crypto=tron.native_coin
            )
        )
        self.assertTrue(
            ChainProductCapabilityService.supports_deposit_address(
                chain=tron, crypto=self.usdt
            )
        )
        self.assertFalse(
            ChainProductCapabilityService.supports_deposit_address(
                chain=tron, crypto=other_trc20
            )
        )

    def test_differ_supports_native_only_on_tron(self):
        # 钱包直收：原生币仅 Tron 可观测（EOA 收原生靠逐块 TransferContract 扫描），EVM 不可。
        from chains.models import ChainType

        self.assertTrue(
            ChainProductCapabilityService.differ_supports_native(
                chain_type=ChainType.TRON
            )
        )
        self.assertFalse(
            ChainProductCapabilityService.differ_supports_native(
                chain_type=ChainType.EVM
            )
        )


class ChainNativeCryptoMappingTests(TestCase):
    def test_creating_chain_auto_creates_native_crypto_mapping(self):
        chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=False,
        )
        native_coin = chain.native_coin

        native_mapping = CryptoOnChain.objects.get(crypto=native_coin, chain=chain)
        self.assertEqual(native_mapping.address, "")
        # 原生币精度以 CryptoOnChain 为唯一真相，取自链的 ChainSpec（ETH=18）。
        self.assertEqual(native_mapping.decimals, chain.spec.native_coin_decimals)


class CryptoOnChainImmutabilityTests(TestCase):
    """CryptoOnChain 的「地址↔币」身份定死：crypto/chain 创建后不可经 save() 变更。"""

    def setUp(self):
        self.chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=False,
        )
        self.usdt = Crypto.objects.create(
            name="Tether", symbol="USDT", coingecko_id="tether"
        )
        self.usdc = Crypto.objects.create(
            name="USD Coin", symbol="USDC", coingecko_id="usd-coin"
        )
        self.token = CryptoOnChain.objects.create(
            crypto=self.usdt,
            chain=self.chain,
            address=Web3.to_checksum_address("0x" + "11" * 20),
            decimals=6,
        )

    def test_changing_crypto_via_save_is_rejected(self):
        self.token.crypto = self.usdc
        with self.assertRaises(ValidationError):
            self.token.save()

        self.token.refresh_from_db()
        self.assertEqual(self.token.crypto_id, self.usdt.id)

    def test_changing_decimals_via_save_is_allowed(self):
        # 精度等非身份字段可正常更新，守卫只锁 crypto/chain。
        self.token.decimals = 8
        self.token.save(update_fields=["decimals"])

        self.token.refresh_from_db()
        self.assertEqual(self.token.decimals, 8)

    def test_evm_contract_address_is_normalized_to_checksum(self):
        raw_address = "0x" + "22" * 20
        mapping = CryptoOnChain.objects.create(
            crypto=self.usdc,
            chain=self.chain,
            address=raw_address.lower(),
            decimals=6,
        )

        self.assertEqual(mapping.address, Web3.to_checksum_address(raw_address))

    def test_tron_hex41_contract_address_is_normalized_to_base58(self):
        from common.tron_codec import TronAddressCodec

        tron = Chain.objects.create(
            code=ChainCode.Tron,
            tron_api_key="",
            active=False,
        )
        base58_address = "TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj"
        mapping = CryptoOnChain.objects.create(
            crypto=self.usdc,
            chain=tron,
            address=TronAddressCodec.base58_to_hex41(base58_address),
            decimals=6,
        )

        self.assertEqual(mapping.address, base58_address)

    def test_invalid_contract_address_is_rejected_on_save(self):
        mapping = CryptoOnChain(
            crypto=self.usdc,
            chain=self.chain,
            address="not-an-address",
            decimals=6,
        )

        with self.assertRaises(ValidationError):
            mapping.save()

    def test_merge_update_path_bypasses_guard(self):
        # QuerySet.update() 不触发 save()，故能绕过身份不可变守卫；本用例固定该旁路事实，
        # 以便后续若有受控的 crypto 改写入口可据此实现。
        CryptoOnChain.objects.filter(pk=self.token.pk).update(crypto=self.usdc)

        self.token.refresh_from_db()
        self.assertEqual(self.token.crypto_id, self.usdc.id)


class MetadataEndpointTests(TestCase):
    """/v1/metadata 公开端点：单一来源下发链/币基础字典给支付页。

    只固定「行为正确性」：公开可访问、只暴露 active 资产、返回结构契约；
    不断言具体 icon URL 字面量（属配置数值，按项目约定不纳入测试）。
    """

    def setUp(self):
        # 端点使用模块级缓存（也是限流后端），逐用例清空避免跨用例命中污染。
        cache.clear()
        self.factory = APIRequestFactory()

    def fetch(self):
        request = self.factory.get("/v1/metadata")
        return MetadataView.as_view()(request)

    def test_public_access_without_auth(self):
        # 无任何鉴权头也应放行（AllowAny），且返回 chains / cryptos 两个键。
        make_evm_chain(code=ChainCode.Ethereum, active=True)
        response = self.fetch()
        self.assertEqual(response.status_code, 200)
        self.assertIn("chains", response.data)
        self.assertIn("cryptos", response.data)

    def test_only_active_chains_and_cryptos_returned(self):
        # 停用的链/币不应出现在支付页可选项里，与正式入口对 active 的门禁一致。
        make_evm_chain(code=ChainCode.Ethereum, active=True)
        make_evm_chain(code=ChainCode.Sepolia, active=False)
        Crypto.objects.create(
            name="Tether", symbol="USDT", active=True, coingecko_id="tether"
        )
        Crypto.objects.create(name="Disabled Coin", symbol="DEAD", active=False)

        response = self.fetch()

        chain_codes = {item["code"] for item in response.data["chains"]}
        self.assertIn(ChainCode.Ethereum, chain_codes)
        self.assertNotIn(ChainCode.Sepolia, chain_codes)

        crypto_symbols = {item["symbol"] for item in response.data["cryptos"]}
        self.assertIn("USDT", crypto_symbols)
        self.assertNotIn("DEAD", crypto_symbols)

    def test_response_contract_fields(self):
        # 前端依赖的字段契约：chains 含 code/name/icon/is_testnet，cryptos 含 symbol/name/icon/is_native。
        make_evm_chain(code=ChainCode.Ethereum, active=True)
        Crypto.objects.create(
            name="Tether", symbol="USDT", active=True, coingecko_id="tether"
        )

        response = self.fetch()

        chain = next(
            item
            for item in response.data["chains"]
            if item["code"] == ChainCode.Ethereum
        )
        self.assertEqual(set(chain), {"code", "name", "icon", "is_testnet"})
        self.assertEqual(chain["name"], "Ethereum")
        self.assertFalse(chain["is_testnet"])

        crypto = next(
            item for item in response.data["cryptos"] if item["symbol"] == "USDT"
        )
        self.assertEqual(set(crypto), {"symbol", "name", "icon", "is_native"})


class StalePriceTests(TestCase):
    """陈旧行情必须停止计价，而不是被无限期沿用。

    CoinGecko 免费公共接口被限流数小时是常态，刷新任务失败只写日志后返回。
    若没有新鲜度校验，这期间所有账单都会按过期汇率换算 pay_amount，买家实付的
    法币价值随行情漂移而系统性偏离。
    """

    def setUp(self):
        cache.clear()
        self.crypto = Crypto.objects.create(
            name="Ethereum",
            symbol="ETH",
            prices={"USD": "3000"},
            coingecko_id="ethereum-stale-test",
            prices_updated_at=timezone.now(),
        )

    def tearDown(self):
        cache.clear()
        super().tearDown()

    def test_fresh_price_is_usable(self):
        self.assertEqual(self.crypto.price("USD"), Decimal("3000"))

    def test_stale_price_raises_instead_of_being_used(self):
        Crypto.objects.filter(pk=self.crypto.pk).update(
            prices_updated_at=timezone.now() - timedelta(hours=6)
        )
        self.crypto.refresh_from_db()

        with self.assertRaises(PriceUnavailableError):
            self.crypto.price("USD")

    def test_missing_timestamp_does_not_break_existing_rows(self):
        """存量数据尚未回填时间戳时不判过期，避免升级瞬间所有币停摆。"""
        Crypto.objects.filter(pk=self.crypto.pk).update(prices_updated_at=None)
        self.crypto.refresh_from_db()

        self.assertEqual(self.crypto.price("USD"), Decimal("3000"))

    def test_zero_threshold_disables_staleness_check(self):
        """阈值 0 是行情源长期不可用时的应急开关。"""
        SystemSettings.objects.create(crypto_price_max_age_seconds=0)
        Crypto.objects.filter(pk=self.crypto.pk).update(
            prices_updated_at=timezone.now() - timedelta(days=30)
        )
        self.crypto.refresh_from_db()

        self.assertEqual(self.crypto.price("USD"), Decimal("3000"))

    def test_usd_pegged_stablecoin_unaffected_by_staleness(self):
        """稳定币对 USD 恒按 1 锚定，不依赖行情，不能被新鲜度校验误伤。"""
        usdt = Crypto.objects.create(
            name="Tether",
            symbol="USDT",
            prices={},
            coingecko_id="tether-stale-test",
            prices_updated_at=timezone.now() - timedelta(days=30),
        )

        self.assertEqual(usdt.price("USD"), Decimal("1"))


class PriceRefreshTimestampTests(TestCase):
    """刷新任务必须让时间戳与价格同批推进，且不能给空结果盖新时间戳。"""

    def setUp(self):
        cache.clear()
        Fiat.objects.get_or_create(code="USD")
        self.crypto = Crypto.objects.create(
            name="Ethereum",
            symbol="ETH",
            prices={},
            coingecko_id="ethereum",
        )

    def tearDown(self):
        cache.clear()
        super().tearDown()

    @patch("currencies.tasks.httpx.get")
    def test_successful_refresh_records_timestamp(self, mock_get):
        mock_get.return_value = Mock(
            raise_for_status=Mock(),
            json=Mock(return_value={"ethereum": {"usd": 3200}}),
        )

        refresh_crypto_prices()

        self.crypto.refresh_from_db()
        self.assertEqual(self.crypto.prices["USD"], 3200)
        self.assertIsNotNone(self.crypto.prices_updated_at)

    @patch("currencies.tasks.httpx.get")
    def test_empty_result_does_not_refresh_timestamp(self, mock_get):
        """行情源没返回该币报价时不能推进时间戳，否则陈旧价被伪装成刚刷新过。"""
        stale_moment = timezone.now() - timedelta(hours=6)
        Crypto.objects.filter(pk=self.crypto.pk).update(
            prices={"USD": "3000"}, prices_updated_at=stale_moment
        )
        mock_get.return_value = Mock(
            raise_for_status=Mock(),
            json=Mock(return_value={}),
        )

        refresh_crypto_prices()

        self.crypto.refresh_from_db()
        self.assertEqual(
            self.crypto.prices_updated_at.replace(microsecond=0),
            stale_moment.replace(microsecond=0),
        )
        # 时间戳没被推进，因此该价格仍然会被判为陈旧。
        self.assertTrue(self.crypto.price_is_stale())
