from __future__ import annotations

from decimal import ROUND_UP
from decimal import Decimal
from functools import cached_property

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from chains.constants import NATIVE_COIN_SYMBOLS
from chains.models import Chain
from common.utils.math import round_decimal


class PriceUnavailableError(Exception):
    """无法取得某币在指定法币下的价格。

    用于把"没有价格源"这一确定的业务态从裸 KeyError 中区分出来：
    充/提币可据此优雅降级（worth 记 0），支付入口据此把该币排除出可选方式。
    """


class Crypto(models.Model):
    name = models.CharField(_("名称"), unique=True)
    symbol = models.CharField(_("代码"), help_text=_("例如:ETH、USDT"), unique=True)
    # M2M 关联，通过 ChainToken 中间表，保存合约地址和链特定精度等部署信息
    chains = models.ManyToManyField(
        Chain,
        through="ChainToken",
        related_name="cryptos",
        verbose_name=_("支持的链"),
        blank=True,
    )
    prices = models.JSONField(_("价格"), default=dict, blank=True)
    # CoinGecko 的全局唯一行情 slug（如 tether/binancecoin），与 symbol 无机械对应。
    # 可空：coingecko_id 只是众多取价来源之一（CoinGecko 行情），并非币的身份。未上
    # CoinGecko 的币（如项目方自定义代币）允许留空——它们仍可充/提币，只是没有法币价格，
    # 故不会出现在按法币计价的支付（invoice）选项里（见 is_payable 与支付能力门禁）。
    # NULL 让多条无 slug 的币并存而不撞唯一约束；空串在 save() 中归一为 NULL，避免空串互撞。
    coingecko_id = models.CharField(_("CoinGecko ID"), unique=True, blank=True, null=True)
    active = models.BooleanField(_("启用"), default=True)
    # 是否为链原生币，建币时定死，运行期作为唯一真相（取代旧的硬编码符号名单）。
    is_native = models.BooleanField(_("原生币"), default=False)

    class Meta:
        verbose_name = _("加密货币")
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.symbol}"

    def save(self, *args, **kwargs):
        # 空 coingecko_id 归一为 NULL：CharField 表单提交的是空串，但唯一约束下多条空串
        # 会互撞，而多个 NULL 可并存（无 CoinGecko 的自定义代币就该是 NULL）。
        if not self.coingecko_id:
            self.coingecko_id = None
        super().save(*args, **kwargs)

    def clean(self) -> None:
        # 兜底校验：标记为原生币的 Crypto，symbol 必须落在系统已知的链原生币集合内，
        # 防止把任意代币误标为原生币（原生币集合权威来源是 CHAIN_SPECS）。
        super().clean()
        if self.is_native and self.symbol not in NATIVE_COIN_SYMBOLS:
            raise ValidationError(
                {
                    "is_native": _(
                        "仅已知链原生币可标记为原生币，允许的符号：%(symbols)s"
                    )
                    % {"symbols": ", ".join(sorted(NATIVE_COIN_SYMBOLS))}
                }
            )

    def get_decimals(self, chain: Chain) -> int:
        """获取代币在指定链上的精度，以 ChainToken 部署记录为唯一来源。

        精度本质是「币×链」的部署属性（如 USDT 在 ETH/Tron 为 6、BSC 为 18），
        统一存于 ChainToken.decimals；未在该链登记部署时视为不可用，抛出 DoesNotExist。
        """
        return ChainToken.objects.get(crypto=self, chain=chain).decimals

    def supported_chains(self) -> str:
        return ", ".join(self.chains.values_list("name", flat=True))

    @classmethod
    def all_methods(cls):
        # 通过 ChainToken 统一处理原生币和合约币，不再区分两种路径
        methods = {}
        for crypto in cls.objects.prefetch_related("chain_tokens__chain"):
            chain_codes = [ct.chain.code for ct in crypto.chain_tokens.all()]
            if chain_codes:
                methods[crypto.symbol] = chain_codes
        return methods

    def price(self, fiat):
        # 稳定币对 USD 恒按 1 锚定，无需行情。
        if fiat == "USD" and self.symbol in self.USD_PEGGED_SYMBOLS:
            return Decimal("1")
        # 无价格源（如未上 CoinGecko 的自定义代币）抛明确领域异常，由调用方决定降级或排除，
        # 避免裸 KeyError 在上层被误当成其他错误。
        try:
            return Decimal(self.prices[fiat])
        except KeyError as exc:
            raise PriceUnavailableError(
                f"{self.symbol} 缺少 {fiat} 价格（coingecko_id={self.coingecko_id!r}）"
            ) from exc

    # USD 锚定稳定币的符号集合：price() 对它们恒返回 1，无需依赖行情。
    USD_PEGGED_SYMBOLS = frozenset({"USDT", "USDC", "DAI"})

    def is_payable(self) -> bool:
        """是否具备法币计价能力，可用于按法币计价的支付（invoice）。

        判据是"有没有价格来源"，而非"当前是否已刷到价"：
        - USD 锚定稳定币恒可计价；
        - 配了 coingecko_id 的币由行情任务周期刷新，视为可计价（首次刷新前也算）；
        - 二者皆无（未上 CoinGecko 的自定义代币）则只能充/提币，不进支付选项。
        """
        return self.symbol in self.USD_PEGGED_SYMBOLS or self.coingecko_id is not None

    def usd_amount(self, amount: Decimal) -> Decimal:
        """将代币数量换算为 USD 价值；无价格源时降级为 0（充/提币不因缺价受阻）。"""
        try:
            return amount * self.price("USD")
        except PriceUnavailableError:
            return Decimal("0")

    @cached_property
    def scale(self):
        if scale := cache.get(f"{self.symbol}_scale"):
            return scale
        price_usd = self.price("USD")

        for i in range(-8, 8):
            if price_usd * Decimal("10") ** i > Decimal("0.01"):
                scale = i - 1
                cache.set(f"{self.symbol}_scale", value=scale, timeout=10)
                return scale
        raise ValueError("系统精度超出范围")

    @cached_property
    def differ_step(self):
        return Decimal("10") ** self.scale

    def to_fiat(self, fiat: Fiat, amount: Decimal) -> Decimal:
        return round_decimal(amount * self.price(fiat.code), -4)

    def support_this_chain(self, chain: Chain) -> bool:
        # 通过 M2M chains 字段统一判断，原生币和合约币均在 ChainToken 中有记录
        return self.chains.filter(pk=chain.pk).exists()

    def address(self, chain: Chain) -> str:
        """获取代币在指定链上的合约地址；原生币的 address 为空字符串。"""
        try:
            return ChainToken.objects.get(crypto=self, chain=chain).address
        except ChainToken.DoesNotExist:
            return ""

    @property
    def icon(self):
        icons = {
            "ETH": "https://assets.coingecko.com/coins/images/279/standard/ethereum.png",
            "BNB": "https://assets.coingecko.com/coins/images/825/standard/bnb-icon2_2x.png",
            "USDC": "https://assets.coingecko.com/coins/images/6319/standard/usdc.png",
            "USDT": "https://assets.coingecko.com/coins/images/325/standard/Tether.png",
            "TRX": "https://assets.coingecko.com/coins/images/1094/standard/photo_2026-04-13_09-59-16.png?1776048311",
        }
        return icons.get(self.symbol, "")


class ChainToken(models.Model):
    """记录代币与链的部署关系，包含链上合约地址及该币在本链的精度。

    原生币（ETH 等）也在此建立记录，address 为空字符串，
    以使 support_this_chain 等逻辑能统一通过此表查询。

    「地址↔币」是链上定死的身份事实，故 crypto/chain 一经创建即不可变更
    （见 ensure_mapping_immutable）；确需纠正只能经 QuerySet.update() 这一受控旁路。
    """

    crypto = models.ForeignKey(
        Crypto,
        on_delete=models.CASCADE,
        related_name="chain_tokens",
        verbose_name=_("加密货币"),
    )
    chain = models.ForeignKey(
        Chain,
        on_delete=models.CASCADE,
        related_name="chain_tokens",
        verbose_name=_("链"),
    )
    # 合约地址；原生币为空字符串
    address = models.CharField(_("合约地址"), blank=True, default="", db_index=True)
    # 该币在本链上的精度，必填；它是精度的唯一真相（如 USDT 在 ETH=6、BSC=18）。
    decimals = models.PositiveSmallIntegerField(_("精度"))
    # 部署级开关：可单独停用某「币×链」组合，而不影响该币在其他链或整条链的可用性。
    active = models.BooleanField(_("启用"), default=True)

    class Meta:
        # 统一采用具名 UniqueConstraint，便于数据库约束报错定位和后续约束扩展。
        constraints = [
            models.UniqueConstraint(
                fields=("crypto", "chain"),
                name="uniq_chain_token_crypto_chain",
            ),
            # 同一条链上的同一个合约地址只能映射到一个资产，防止 webhook 解析歧义。
            models.UniqueConstraint(
                fields=("chain", "address"),
                name="uniq_chain_token_chain_address",
            ),
        ]
        verbose_name = _("代币部署")
        verbose_name_plural = _("代币部署")

    def __str__(self):
        return f"{self.crypto.symbol} @ {self.chain.code}"

    def save(self, *args, **kwargs):
        # clean() 不会在 save() 时自动触发，这里直接兜底，挡住绕过表单的程序化改写。
        self.ensure_mapping_immutable()
        super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        self.ensure_mapping_immutable()

    def ensure_mapping_immutable(self) -> None:
        """禁止变更已存在部署的 crypto/chain。

        链上「合约地址 ↔ 币种」是永久不变的身份事实，误改会静默污染历史 Transfer
        的资产归属（且按当前策略不追溯回填），属于不可逆的金融数据事故。新建（pk 为空）
        不受限；确需纠正只能经 QuerySet.update() 旁路本守卫，是唯一受控的变更入口。
        """
        if self.pk is None:
            return
        old = (
            ChainToken.objects.filter(pk=self.pk)
            .values("crypto_id", "chain_id")
            .first()
        )
        if old is None:
            return
        if old["crypto_id"] != self.crypto_id or old["chain_id"] != self.chain_id:
            raise ValidationError(
                _(
                    "代币部署的链与币种一经创建不可变更；确需纠正只能经 QuerySet.update() 受控旁路。"
                )
            )


class Fiat(models.Model):
    code = models.CharField(_("代码"), primary_key=True)

    class Meta:
        verbose_name = _("法币")
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.code} {self.icon}"

    def fiat_price(self, fiat: Fiat) -> Decimal:
        usdt = Crypto.objects.get(symbol="USDT")
        price0 = usdt.price(self.code)
        price1 = usdt.price(fiat.code)

        return price1 / price0

    def to_crypto(self, crypto: Crypto, amount: Decimal) -> Decimal:
        return round_decimal(
            amount / Decimal(crypto.price(self.code)),
            crypto.scale,
            rounding=ROUND_UP,
        )

    @classmethod
    def get(cls, code):
        return Fiat.objects.get(code=code)

    @property
    def icon(self):
        flags = {
            # 亚洲
            "CNY": "🇨🇳",
            "HKD": "🇭🇰",
            "JPY": "🇯🇵",
            "KRW": "🇰🇷",
            "SGD": "🇸🇬",
            "INR": "🇮🇳",
            "THB": "🇹🇭",
            "PHP": "🇵🇭",
            "IDR": "🇮🇩",
            "MYR": "🇲🇾",
            "VND": "🇻🇳",
            "PKR": "🇵🇰",
            "BDT": "🇧🇩",
            "ILS": "🇮🇱",
            "TWD": "🇹🇼",
            # 中东
            "AED": "🇦🇪",
            "SAR": "🇸🇦",
            "KWD": "🇰🇼",
            "QAR": "🇶🇦",
            # 美洲
            "USD": "🇺🇸",
            "CAD": "🇨🇦",
            "BRL": "🇧🇷",
            "MXN": "🇲🇽",
            "ARS": "🇦🇷",
            "CLP": "🇨🇱",
            "COP": "🇨🇴",
            # 欧洲
            "EUR": "🇪🇺",
            "GBP": "🇬🇧",
            "GDB": "🇬🇧",
            "CHF": "🇨🇭",
            "SEK": "🇸🇪",
            "NOK": "🇳🇴",
            "DKK": "🇩🇰",
            "PLN": "🇵🇱",
            "CZK": "🇨🇿",
            "HUF": "🇭🇺",
            "RON": "🇷🇴",
            "BGN": "🇧🇬",
            "RUB": "🇷🇺",
            "TRY": "🇹🇷",
            "UAH": "🇺🇦",
            # 大洋洲
            "AUD": "🇦🇺",
            "NZD": "🇳🇿",
            # 非洲
            "ZAR": "🇿🇦",
            "EGP": "🇪🇬",
            "NGN": "🇳🇬",
        }

        return flags.get(self.code, "")
