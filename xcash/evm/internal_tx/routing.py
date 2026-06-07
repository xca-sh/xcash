from __future__ import annotations

from typing import Protocol

from chains.models import Chain
from chains.models import TxTask
from chains.models import TxTaskType
from evm.internal_tx.facts import MatchedTransferFact


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


from evm.internal_tx.vault_slot_collect import vault_slot_collect_matcher  # noqa: E402

INTERNAL_TX_MATCHERS: dict[TxTaskType, ReceiptMatcher] = {
    TxTaskType.VaultSlotCollect: vault_slot_collect_matcher,
}


def get_matcher(tx_type: TxTaskType) -> ReceiptMatcher:
    """按任务类型获取 receipt matcher；未注册类型抛 KeyError。"""
    return INTERNAL_TX_MATCHERS[tx_type]
