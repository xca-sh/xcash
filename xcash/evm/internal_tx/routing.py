from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from chains.models import Chain
from chains.models import Transfer
from chains.models import TxTask
from chains.models import TxTaskType
from currencies.models import Crypto


class UnknownInternalBroadcastError(RuntimeError):
    """系统内地址发出交易但无法解析到对应 TxTask。"""

    def __init__(self, *, chain_code: str, tx_hash: str, from_address: str):
        super().__init__(
            f"system address {from_address} sent tx {tx_hash} on {chain_code} "
            f"without a resolvable TxTask"
        )
        self.chain_code = chain_code
        self.tx_hash = tx_hash
        self.from_address = from_address


@dataclass(frozen=True)
class MatchedTransferFact:
    """receipt 中提取的真实资产移动事实，由 matcher 返回。"""

    from_address: str
    to_address: str
    crypto: Crypto
    value: Decimal
    amount: Decimal


class ReceiptMatcher(Protocol):
    """从 receipt 中提取与 TxTask 预期吻合的真实资产移动事实。"""

    def __call__(
        self,
        *,
        chain: Chain,
        tx_task: TxTask,
        receipt: dict,
        tx: dict | None = None,
    ) -> MatchedTransferFact | None: ...


class InternalTransferHandler(Protocol):
    """按 TxTaskType 推进系统内主动交易的业务生命周期。"""

    def match(self, transfer: Transfer, tx_task: TxTask) -> bool: ...

    def confirm(self, transfer: Transfer) -> None: ...

    def drop(self, transfer: Transfer) -> None: ...

    def finalize_failed(self, tx_task: TxTask) -> None: ...


class NoopInternalTransferHandler:
    """无业务副作用的本地主动交易生命周期 handler。"""

    def match(self, transfer: Transfer, tx_task: TxTask) -> bool:
        return True

    def confirm(self, transfer: Transfer) -> None:
        return None

    def drop(self, transfer: Transfer) -> None:
        return None

    def finalize_failed(self, tx_task: TxTask) -> None:
        return None


from evm.internal_tx.vault_slot_collect import vault_slot_collect_handler  # noqa: E402
from evm.internal_tx.vault_slot_collect import vault_slot_collect_matcher  # noqa: E402
from evm.internal_tx.withdrawal import withdrawal_handler  # noqa: E402
from evm.internal_tx.withdrawal import withdrawal_matcher  # noqa: E402

noop_internal_transfer_handler = NoopInternalTransferHandler()

NON_TRANSFER_TX_TASK_TYPES: set[TxTaskType] = {
    TxTaskType.VaultSlotDeploy,
}

INTERNAL_TX_HANDLERS: dict[TxTaskType, InternalTransferHandler] = {
    TxTaskType.VaultSlotDeploy: noop_internal_transfer_handler,
    TxTaskType.VaultSlotCollect: vault_slot_collect_handler,
    TxTaskType.Withdrawal: withdrawal_handler,
}

INTERNAL_TX_MATCHERS: dict[TxTaskType, ReceiptMatcher] = {
    TxTaskType.VaultSlotCollect: vault_slot_collect_matcher,
    TxTaskType.Withdrawal: withdrawal_matcher,
}


def get_handler(tx_type: TxTaskType) -> InternalTransferHandler:
    """按任务类型获取业务生命周期 handler；未注册类型抛 KeyError。"""
    return INTERNAL_TX_HANDLERS[tx_type]


def get_matcher(tx_type: TxTaskType) -> ReceiptMatcher:
    """按任务类型获取 receipt matcher；未注册类型抛 KeyError。"""
    return INTERNAL_TX_MATCHERS[tx_type]
