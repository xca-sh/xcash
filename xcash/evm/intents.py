"""EVM 交易意图骨架与轻量校验工具。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal  # noqa: TC003 - 规格要求运行时可解析 Decimal 注解
from typing import TYPE_CHECKING

import eth_abi
from web3 import Web3

from chains.models import TransferType
from evm.choices import TxKind

if TYPE_CHECKING:
    from collections.abc import Callable

    from chains.models import Address
    from chains.models import Chain
    from currencies.models import Crypto


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
    amount: Decimal | None
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


_ERC20_TRANSFER_SELECTOR = "0xa9059cbb"


def build_native_transfer_intent(
    *,
    address: Address,
    chain: Chain,
    to: str,
    value: int,
    transfer_type: TransferType,
    verify_fn: Callable[[], None] | None = None,
) -> EvmTxIntent:
    if value < 0:
        raise ValueError("value must be >= 0")

    to_checksum = Web3.to_checksum_address(to)
    native = chain.native_coin

    return EvmTxIntent(
        address=address,
        chain=chain,
        tx_kind=TxKind.NATIVE_TRANSFER,
        to=to_checksum,
        value=value,
        data="",
        gas=chain.base_transfer_gas,
        transfer_type=transfer_type,
        crypto=native,
        recipient=to_checksum,
        amount=Decimal(value).scaleb(-native.decimals),
        verify_fn=verify_fn,
    )


def build_erc20_transfer_intent(
    *,
    address: Address,
    chain: Chain,
    crypto: Crypto,
    to: str,
    value_raw: int,
    transfer_type: TransferType,
    verify_fn: Callable[[], None] | None = None,
) -> EvmTxIntent:
    if value_raw < 0:
        raise ValueError("value_raw must be >= 0")

    to_checksum = Web3.to_checksum_address(to)
    token_addr = crypto.address(chain)
    if not token_addr:
        raise ValueError(
            f"Crypto {crypto.symbol} is not deployed on chain {chain.code}"
        )

    token_checksum = Web3.to_checksum_address(token_addr)
    encoded_args = eth_abi.encode(["address", "uint256"], [to_checksum, value_raw]).hex()

    return EvmTxIntent(
        address=address,
        chain=chain,
        tx_kind=TxKind.CONTRACT_CALL,
        to=token_checksum,
        value=0,
        data=f"{_ERC20_TRANSFER_SELECTOR}{encoded_args}",
        gas=chain.erc20_transfer_gas,
        transfer_type=transfer_type,
        crypto=crypto,
        recipient=to_checksum,
        amount=Decimal(value_raw).scaleb(-crypto.get_decimals(chain)),
        verify_fn=verify_fn,
    )


def build_contract_call_intent(
    *,
    address: Address,
    chain: Chain,
    contract_address: str,
    data: str,
    gas: int,
    transfer_type: TransferType,
    value: int = 0,
    crypto: Crypto | None = None,
    recipient: str | None = None,
    amount: Decimal | None = None,
    verify_fn: Callable[[], None] | None = None,
) -> EvmTxIntent:
    if gas <= 0:
        raise ValueError("gas must be > 0")
    if value < 0:
        raise ValueError("value must be >= 0")

    return EvmTxIntent(
        address=address,
        chain=chain,
        tx_kind=TxKind.CONTRACT_CALL,
        to=Web3.to_checksum_address(contract_address),
        value=value,
        data=_normalize_hex_calldata(data),
        gas=gas,
        transfer_type=transfer_type,
        crypto=crypto,
        recipient=recipient,
        amount=amount,
        verify_fn=verify_fn,
    )
