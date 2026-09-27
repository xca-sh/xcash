"""充值业务向 chains 引擎登记的实现，由 DepositsConfig.ready() 调用 register()。

每个回调都在调用时才解析 DepositService 的方法，保证测试对这些方法的 patch 生效。
"""

from chains.models import TransferType
from chains.models import VaultSlotUsage
from chains.registry import TransferHandler
from chains.registry import register_transfer_handler
from deposits.service import DepositService

# 充值排在账单之后匹配，与重构前的顺序一致。
DEPOSIT_TRANSFER_MATCH_ORDER = 20


def try_match_transfer(transfer) -> bool:
    return DepositService.try_match_deposit_transfer(transfer)


def try_match_initial_native_balance(transfer, **_context) -> bool:
    # 充值槽位按地址归属客户，初始余额与普通入账走同一条匹配路径，
    # 用不到 slot / payment_datetime 上下文。
    return DepositService.try_match_deposit_transfer(transfer)


def confirm_transfer(transfer) -> None:
    DepositService.create_confirmed_deposit(transfer)


def register() -> None:
    register_transfer_handler(
        TransferHandler(
            transfer_type=TransferType.Deposit,
            vault_slot_usage=VaultSlotUsage.DEPOSIT,
            match_order=DEPOSIT_TRANSFER_MATCH_ORDER,
            try_match=try_match_transfer,
            try_match_initial_native_balance=try_match_initial_native_balance,
            confirm=confirm_transfer,
        )
    )
