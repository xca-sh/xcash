"""账单业务向 chains 引擎登记的实现，由 InvoicesConfig.ready() 调用 register()。

每个回调都在调用时才解析 InvoiceService / DifferRecipientAddress 的方法，
保证测试对这些方法的 patch 生效（登记的是本模块函数，不是方法引用）。
"""

from chains.models import TransferType
from chains.models import VaultSlotUsage
from chains.registry import RecipientAddressSource
from chains.registry import TransferHandler
from chains.registry import register_recipient_address_source
from chains.registry import register_transfer_handler
from invoices.models import DifferRecipientAddress
from invoices.service import InvoiceService

# 账单先于充值匹配：沿用重构前 try_match_invoice(...) or try_match_deposit(...) 的顺序。
INVOICE_TRANSFER_MATCH_ORDER = 10


def try_match_transfer(transfer) -> bool:
    return InvoiceService.try_match_invoice(transfer)


def try_match_initial_native_balance(transfer, *, slot, payment_datetime) -> bool:
    return InvoiceService.try_match_vault_slot_initial_native_balance(
        transfer=transfer,
        slot=slot,
        payment_datetime=payment_datetime,
    )


def confirm_transfer(transfer) -> None:
    InvoiceService.confirm_invoice(transfer.invoice)


def match_differ_recipient_addresses(*, chain, candidates: set[str]) -> set[str]:
    """钱包直收地址是商户自有 EOA，不是 VaultSlot，扫描器需要单独识别。"""
    return DifferRecipientAddress.matched_addresses_for_candidates(
        chain=chain,
        candidates=candidates,
    )


def register() -> None:
    register_transfer_handler(
        TransferHandler(
            transfer_type=TransferType.Invoice,
            vault_slot_usage=VaultSlotUsage.INVOICE,
            match_order=INVOICE_TRANSFER_MATCH_ORDER,
            try_match=try_match_transfer,
            try_match_initial_native_balance=try_match_initial_native_balance,
            confirm=confirm_transfer,
        )
    )
    register_recipient_address_source(
        RecipientAddressSource(
            name="invoices.differ_recipient_address",
            match_addresses=match_differ_recipient_addresses,
        )
    )
