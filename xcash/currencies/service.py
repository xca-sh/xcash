from __future__ import annotations

import typing

from chains.capabilities import ChainProductCapabilityService
from currencies.models import ChainToken
from currencies.models import Crypto
from currencies.models import Fiat

if typing.TYPE_CHECKING:
    from decimal import Decimal

    from django.db.models import QuerySet


class CryptoService:
    """集中封装 Crypto 模型的常见读写操作。"""

    @staticmethod
    def list_all(*, active_only: bool = True) -> QuerySet[Crypto]:
        # active 是币的启用开关：停用的币默认不暴露给正式业务入口，仅后台可显式查看。
        queryset = Crypto.objects.all()
        if active_only:
            queryset = queryset.filter(active=True)
        return queryset

    @staticmethod
    def get_by_symbol(symbol: str, *, active_only: bool = True) -> Crypto:
        # 正式业务默认只允许读取已激活资产；后台治理场景可显式放开 active_only=False。
        queryset = Crypto.objects.filter(symbol=symbol)
        if active_only:
            queryset = queryset.filter(active=True)
        return queryset.get()

    @staticmethod
    def exists(symbol: str, *, active_only: bool = True) -> bool:
        # 停用的币不应被 invoice / withdrawal / deposit 地址申请等正式入口识别为可用资产。
        queryset = Crypto.objects.filter(symbol=symbol)
        if active_only:
            queryset = queryset.filter(active=True)
        return queryset.exists()

    @staticmethod
    def price(crypto: Crypto, fiat_code: str) -> Decimal:
        return crypto.price(fiat_code)

    @staticmethod
    def to_fiat(crypto: Crypto, fiat: Fiat, amount: Decimal) -> Decimal:
        return crypto.to_fiat(fiat, amount)

    @staticmethod
    def allowed_methods(*, chain_codes: set[str] | None = None) -> dict[str, set[str]]:
        """返回系统级 invoice 可用 (crypto_symbol → {chain_code}) 映射。

        实现：通过 ChainToken 一次查询带出所有 active 的部署关系（币、链、部署三级开关
        均需启用），在内存中应用 capability 规则。chain_codes 可把查询收敛到项目已配置
        收币地址的链，避免无关链币关系进入后续计算。
        """
        tokens = ChainToken.objects.select_related("crypto", "chain").filter(
            crypto__active=True, chain__active=True, active=True
        )
        if chain_codes is not None:
            tokens = tokens.filter(chain__code__in=chain_codes)

        sanitized: dict[str, set[str]] = {}
        for token in tokens:
            if ChainProductCapabilityService.supports_existing_invoice_method(
                chain=token.chain,
                crypto=token.crypto,
            ):
                sanitized.setdefault(token.crypto.symbol, set()).add(token.chain.code)

        return sanitized


class FiatService:
    """封装法币模型的查询与转换逻辑。"""

    @staticmethod
    def list_all() -> QuerySet[Fiat]:
        return Fiat.objects.all()

    @staticmethod
    def get_by_code(code: str) -> Fiat:
        return Fiat.objects.get(code=code)

    @staticmethod
    def exists(code: str) -> bool:
        return Fiat.objects.filter(code=code).exists()

    @staticmethod
    def to_crypto(fiat: Fiat, crypto: Crypto, amount: Decimal) -> Decimal:
        return fiat.to_crypto(crypto, amount)

    @staticmethod
    def fiat_price(fiat: Fiat, target: Fiat) -> Decimal:
        return fiat.fiat_price(target)
