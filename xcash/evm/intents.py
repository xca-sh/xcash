"""EVM 交易意图骨架与轻量校验工具。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from chains.models import TransferType
from evm.choices import TxKind

if TYPE_CHECKING:
    from collections.abc import Callable

    from tokens.models import Crypto

    from chains.models import Chain
    from evm.models import Address


@dataclass(frozen=True)
class EvmTxIntent:
    """描述一笔待构建的 EVM 交易，不在本层实现具体 builder。"""

    address: Address
    chain: Chain
    tx_kind: TxKind
    to: str
    value: int
    data: str
    gas: int
    transfer_type: TransferType
    crypto: Crypto | None
    recipient: str | None
    amount: int | None
    verify_fn: Callable[[], None] | None = None


_PREFLIGHT_BUFFER_MULTIPLIER = {
    TxKind.NATIVE_TRANSFER: 2,
    TxKind.CONTRACT_CALL: 2,
}

_GAS_RECHARGEABLE_TRANSFER_TYPES = frozenset({TransferType.DepositCollection})

_UNIMPLEMENTED_BUSINESS_TRANSFER_TYPES = frozenset(
    {
        TransferType.X402Facilitate,
        TransferType.ContractDeployCollect,
    }
)


def _normalize_hex_calldata(data: str) -> str:
    """规范化 calldata 十六进制字符串，保持字节边界完整。"""
    if data in {"", "0x"}:
        return "0x"

    normalized = data.lower()
    if not normalized.startswith("0x"):
        normalized = f"0x{normalized}"

    hex_body = normalized[2:]
    if len(hex_body) % 2 != 0:
        raise ValueError("calldata must be an even-length hex string")

    try:
        bytes.fromhex(hex_body)
    except ValueError as exc:
        raise ValueError("calldata must be a hex string") from exc

    return normalized


def _require_bytes32(name: str, value: object) -> None:
    """要求传入值为 32 字节二进制，常用于哈希、salt、nonce 等字段。"""
    if not isinstance(value, bytes | bytearray) or len(value) != 32:
        raise ValueError(f"{name} must be bytes32")


def get_preflight_buffer_multiplier(tx_kind: TxKind) -> int:
    return _PREFLIGHT_BUFFER_MULTIPLIER[tx_kind]


def is_gas_rechargeable(transfer_type: TransferType) -> bool:
    return transfer_type in _GAS_RECHARGEABLE_TRANSFER_TYPES


def assert_transfer_type_implemented(transfer_type: TransferType) -> None:
    if transfer_type in _UNIMPLEMENTED_BUSINESS_TRANSFER_TYPES:
        raise NotImplementedError(
            f"{transfer_type.label} EVM builder will be implemented in §11"
        )
