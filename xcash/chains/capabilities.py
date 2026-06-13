from __future__ import annotations

from tron.config import tron_vault_slot_runtime_ready

from chains.models import ChainType


class ChainProductCapabilityService:
    """集中维护链类型在各产品入口中的能力边界。"""

    INVOICE_RECIPIENT_CHAIN_TYPES = frozenset({ChainType.EVM, ChainType.TRON})
    DEPOSIT_CHAIN_TYPES = frozenset({ChainType.EVM})

    @classmethod
    def supports_existing_invoice_method(cls, *, chain, crypto) -> bool:
        """判断已存在 CryptoOnChain 关系的链币组合是否可用于 Invoice。"""
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
            return crypto.symbol == "USDT" or crypto.is_native
        return True

    @classmethod
    def supports_deposit_address(cls, *, chain, crypto) -> bool:
        if not crypto.support_this_chain(chain):
            return False
        if chain.type in cls.DEPOSIT_CHAIN_TYPES:
            return True
        if chain.type == ChainType.TRON:
            # Tron VaultSlot 已覆盖 TRC20 与原生 TRX：TRC20 走 Transfer 事件扫描，
            # 原生 TRX 走 TransferContract 扫描并在部署后 collect(address(0)) 归集。
            supported_asset = crypto.symbol == "USDT" or crypto.is_native
            return supported_asset and tron_vault_slot_runtime_ready()
        return False

    @classmethod
    def differ_supports_native(cls, *, chain_type: str) -> bool:
        """钱包直收地址是普通 EOA，原生币能否被观测取决于该链的扫描机制。

        Tron 逐块扫 TransferContract（filter_matched_events 含 DifferRecipientAddress 匹配），
        原生 TRX 打到 EOA 也能观测；EVM 靠合约事件，原生币打到 EOA 不触发合约、零事件、
        物理不可观测，故钱包直收只在 Tron 上开放原生币。
        """
        return chain_type == ChainType.TRON
