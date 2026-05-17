from __future__ import annotations

from typing import Protocol

from chains.models import BroadcastTask, Chain, OnchainActionType
from evm.internal_tx.facts import MatchedTransferFact


class ReceiptMatcher(Protocol):
    """从 receipt 中提取与 BroadcastTask 预期吻合的真实资产移动事实。"""

    def __call__(
        self,
        *,
        chain: Chain,
        broadcast_task: BroadcastTask,
        receipt: dict,
    ) -> MatchedTransferFact | None: ...


MATCHERS: dict[OnchainActionType, ReceiptMatcher] = {}


def get_matcher(action_type: OnchainActionType) -> ReceiptMatcher:
    return MATCHERS[action_type]


from evm.internal_tx.deposit_collection import deposit_collection_matcher  # noqa: E402
from evm.internal_tx.create2 import create2_matcher  # noqa: E402
from evm.internal_tx.gas_recharge import gas_recharge_matcher  # noqa: E402
from evm.internal_tx.withdrawal import withdrawal_matcher  # noqa: E402
from evm.internal_tx.x402 import x402_matcher  # noqa: E402

MATCHERS[OnchainActionType.Withdrawal] = withdrawal_matcher
MATCHERS[OnchainActionType.GasRecharge] = gas_recharge_matcher
MATCHERS[OnchainActionType.DepositCollection] = deposit_collection_matcher
MATCHERS[OnchainActionType.X402Facilitate] = x402_matcher
MATCHERS[OnchainActionType.ContractDeployCollect] = create2_matcher
