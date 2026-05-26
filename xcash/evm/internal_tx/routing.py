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

    event_id: str
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


from evm.internal_tx.deposit_slot_collect import deposit_slot_collect_handler  # noqa: E402
from evm.internal_tx.deposit_slot_collect import deposit_slot_collect_matcher  # noqa: E402
from evm.internal_tx.withdrawal import withdrawal_handler  # noqa: E402
from evm.internal_tx.withdrawal import withdrawal_matcher  # noqa: E402

INTERNAL_TX_HANDLERS: dict[TxTaskType, InternalTransferHandler] = {
    TxTaskType.DepositSlotCollect: deposit_slot_collect_handler,
    TxTaskType.Withdrawal: withdrawal_handler,
}

INTERNAL_TX_MATCHERS: dict[TxTaskType, ReceiptMatcher] = {
    TxTaskType.DepositSlotCollect: deposit_slot_collect_matcher,
    TxTaskType.Withdrawal: withdrawal_matcher,
}


def get_handler(tx_type: TxTaskType) -> InternalTransferHandler:
    return INTERNAL_TX_HANDLERS[tx_type]


def get_matcher(tx_type: TxTaskType) -> ReceiptMatcher:
    return INTERNAL_TX_MATCHERS[tx_type]
