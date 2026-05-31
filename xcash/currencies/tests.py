from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from web3 import Web3

from chains.capabilities import ChainProductCapabilityService
from chains.constants import ChainCode
from chains.models import Chain
from currencies.models import ChainToken
from currencies.models import Crypto
from currencies.models import PriceUnavailableError


class CustomTokenPricingTests(TestCase):
    """未上 CoinGecko 的自定义代币：可充提币、不进支付、价格优雅降级。"""

    def setUp(self):
        self.chain = Chain.objects.create(code=ChainCode.Ethereum, rpc="", active=True)
        # 无 coingecko_id 的自定义代币
        self.custom = Crypto.objects.create(name="ProjectCoin", symbol="PJC")
        # 有行情源的币（稳定币锚定 USD）
        self.usdt = Crypto.objects.create(
            name="Tether", symbol="USDT", coingecko_id="tether"
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
        # 充/提币用的 usd_amount 在缺价时降级为 0，不阻断业务。
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


class ChainNativeCryptoMappingTests(TestCase):
    def test_creating_chain_auto_creates_native_crypto_mapping(self):
        chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
        native_coin = chain.native_coin

        native_mapping = ChainToken.objects.get(crypto=native_coin, chain=chain)
        self.assertEqual(native_mapping.address, "")
        # 原生币精度以 ChainToken 为唯一真相，取自链的 ChainSpec（ETH=18）。
        self.assertEqual(native_mapping.decimals, chain.spec.native_coin_decimals)


class ChainTokenImmutabilityTests(TestCase):
    """ChainToken 的「地址↔币」身份定死：crypto/chain 创建后不可经 save() 变更。"""

    def setUp(self):
        self.chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
        self.usdt = Crypto.objects.create(
            name="Tether", symbol="USDT", coingecko_id="tether"
        )
        self.usdc = Crypto.objects.create(
            name="USD Coin", symbol="USDC", coingecko_id="usd-coin"
        )
        self.token = ChainToken.objects.create(
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

    def test_merge_update_path_bypasses_guard(self):
        # QuerySet.update() 不触发 save()，故能绕过身份不可变守卫；本用例固定该旁路事实，
        # 以便后续若有受控的 crypto 改写入口可据此实现。
        ChainToken.objects.filter(pk=self.token.pk).update(crypto=self.usdc)

        self.token.refresh_from_db()
        self.assertEqual(self.token.crypto_id, self.usdc.id)
