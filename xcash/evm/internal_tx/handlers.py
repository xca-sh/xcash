from __future__ import annotations

from typing import Protocol

from chains.models import (
    BroadcastTask,
    BroadcastTaskFailureReason,
    OnchainActionType,
    OnchainTransfer,
)


class InternalTransferHandler(Protocol):
    """按 OnchainActionType 推进系统内交易的业务生命周期。"""

    def match(self, transfer: OnchainTransfer, broadcast_task: BroadcastTask) -> bool: ...

    def confirm(self, transfer: OnchainTransfer) -> None: ...

    def drop(self, transfer: OnchainTransfer) -> None: ...

    def finalize_failed(
        self,
        broadcast_task: BroadcastTask,
        reason: BroadcastTaskFailureReason,
    ) -> None: ...


HANDLERS: dict[OnchainActionType, InternalTransferHandler] = {}


def get_handler(action_type: OnchainActionType) -> InternalTransferHandler:
    return HANDLERS[action_type]


from evm.internal_tx.deposit_collection import deposit_collection_handler  # noqa: E402
from evm.internal_tx.create2 import contract_deploy_collection_handler  # noqa: E402
from evm.internal_tx.gas_recharge import gas_recharge_handler  # noqa: E402
from evm.internal_tx.withdrawal import withdrawal_handler  # noqa: E402
from evm.internal_tx.x402 import x402_handler  # noqa: E402

HANDLERS[OnchainActionType.Withdrawal] = withdrawal_handler
HANDLERS[OnchainActionType.GasRecharge] = gas_recharge_handler
HANDLERS[OnchainActionType.DepositCollection] = deposit_collection_handler
HANDLERS[OnchainActionType.X402Facilitate] = x402_handler
HANDLERS[OnchainActionType.ContractDeployCollect] = contract_deploy_collection_handler
