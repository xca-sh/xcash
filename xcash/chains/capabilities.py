from __future__ import annotations

from chains.models import ChainType


class ChainProductCapabilityService:
    """集中维护链类型在各产品入口中的能力边界。"""

    INVOICE_RECIPIENT_CHAIN_TYPES = frozenset({ChainType.EVM, ChainType.TRON})
    DEPOSIT_CHAIN_TYPES = frozenset({ChainType.EVM})

    @staticmethod
    def has_active_crypto_on_chain(*, chain, crypto) -> bool:
        """币、链关系启用才允许进入真实收款入口。

        Crypto.active 与 Chain.active 只能表达全局开关；CryptoOnChain.active 才是某个
        「币×链」组合能否被扫描器处理的开关。公开收款入口必须和扫描器使用同一条件，
        否则会返回一个系统不会入账的充值/支付指引。
        """
        return crypto.crypto_on_chains.filter(chain=chain, active=True).exists()

    @classmethod
    def supports_existing_invoice_method(cls, *, chain, crypto) -> bool:
        """判断已存在 CryptoOnChain 关系的链币组合是否可用于 Invoice。"""
        if not cls.has_active_crypto_on_chain(chain=chain, crypto=crypto):
            return False
        if chain.type not in cls.INVOICE_RECIPIENT_CHAIN_TYPES:
            return False
        # 支付按法币计价，必须有价格来源；无价格源的币（如未上 CoinGecko 的自定义代币）
        # 只能用于非支付资产流转，不进支付选项，否则建单时 to_fiat/to_crypto 会因缺价失败。
        if not crypto.is_payable():
            return False
        if chain.type == ChainType.TRON:
            # Tron 账单收款放行 USDT（主流 TRC20）与原生 TRX；原生 TRX 的入账扫描
            # （逐块 TransferContract）与归集（部署后 collect(address(0))）已就绪。
            # 其余 TRC20 暂不作为账单支付方式。
            return crypto.symbol == "USDT" or cls.is_chain_native_coin(
                chain=chain, crypto=crypto
            )
        return True

    @classmethod
    def supports_deposit_address(cls, *, chain, crypto) -> bool:
        if not cls.has_active_crypto_on_chain(chain=chain, crypto=crypto):
            return False
        if chain.type in cls.DEPOSIT_CHAIN_TYPES:
            return True
        if chain.type == ChainType.TRON:
            # Tron VaultSlot 已覆盖 TRC20 与原生 TRX：TRC20 走 Transfer 事件扫描，
            # 原生 TRX 走 TransferContract 扫描并在部署后 collect(address(0)) 归集。
            return crypto.symbol == "USDT" or cls.is_chain_native_coin(
                chain=chain, crypto=crypto
            )
        return False

    @staticmethod
    def is_chain_native_coin(*, chain, crypto) -> bool:
        """是否为本链原生币。

        不能用 Crypto.is_native：它表示该币是某条链的原生币，ETH/BNB 等以 TRC20
        形式上架 Tron 时同样为真，会被当成原生 TRX 放行收款、并按 address(0) 归集。
        """
        return crypto.pk == chain.native_coin.pk

    @classmethod
    def differ_supports_native(cls, *, chain_type: str) -> bool:
        """钱包直收地址是普通 EOA，原生币能否被观测取决于该链的扫描机制。

        Tron 逐块扫 TransferContract（filter_matched_events 含 DifferRecipientAddress 匹配），
        原生 TRX 打到 EOA 也能观测；EVM 靠合约事件，原生币打到 EOA 不触发合约、零事件、
        物理不可观测，故钱包直收只在 Tron 上开放原生币。
        """
        return chain_type == ChainType.TRON
