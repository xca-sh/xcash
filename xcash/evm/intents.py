"""EVM 交易意图骨架与轻量校验工具。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal  # noqa: TC003 - 规格要求运行时可解析 Decimal 注解
from typing import TYPE_CHECKING

import eth_abi
from web3 import Web3

from chains.models import TransferType
from evm.choices import TxKind
from evm.constants import get_x402_eip3009_facilitate_gas

if TYPE_CHECKING:
    from collections.abc import Callable

    from chains.models import Address
    from chains.models import Chain
    from currencies.models import Crypto


@dataclass(frozen=True)
class EvmTxIntent:
    """由 builder 构造并传给 schedule 的 EVM 交易入参容器。"""

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

    recipient_checksum = Web3.to_checksum_address(recipient) if recipient else None

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
        recipient=recipient_checksum,
        amount=amount,
        verify_fn=verify_fn,
    )


_EIP3009_TRANSFER_WITH_AUTH_SELECTOR = "0xe3ee160e"


@dataclass(frozen=True)
class X402Authorization:
    pass


@dataclass(frozen=True)
class Eip3009Authorization(X402Authorization):
    from_address: str
    to: str
    value: int
    valid_after: int
    valid_before: int
    nonce: bytes
    v: int
    r: bytes
    s: bytes


def build_x402_eip3009_facilitate_intent(
    *,
    address: Address,
    chain: Chain,
    crypto: Crypto,
    authorization: Eip3009Authorization,
) -> EvmTxIntent:
    if authorization.value < 0:
        raise ValueError("authorization.value must be >= 0")
    if authorization.valid_after >= authorization.valid_before:
        raise ValueError("authorization.valid_after must be < authorization.valid_before")

    _require_bytes32("authorization.nonce", authorization.nonce)
    _require_bytes32("authorization.r", authorization.r)
    _require_bytes32("authorization.s", authorization.s)

    if authorization.v not in {27, 28}:
        raise ValueError("authorization.v must be 27 or 28")

    token_addr = crypto.address(chain)
    if not token_addr:
        raise ValueError(
            f"Crypto {crypto.symbol} is not deployed on chain {chain.code}"
        )

    contract_addr = Web3.to_checksum_address(token_addr)
    auth_from = Web3.to_checksum_address(authorization.from_address)
    auth_to = Web3.to_checksum_address(authorization.to)
    encoded_args = eth_abi.encode(
        [
            "address",
            "address",
            "uint256",
            "uint256",
            "uint256",
            "bytes32",
            "uint8",
            "bytes32",
            "bytes32",
        ],
        [
            auth_from,
            auth_to,
            authorization.value,
            authorization.valid_after,
            authorization.valid_before,
            authorization.nonce,
            authorization.v,
            authorization.r,
            authorization.s,
        ],
    ).hex()

    return build_contract_call_intent(
        address=address,
        chain=chain,
        contract_address=contract_addr,
        data=f"{_EIP3009_TRANSFER_WITH_AUTH_SELECTOR}{encoded_args}",
        gas=get_x402_eip3009_facilitate_gas(chain),
        transfer_type=TransferType.X402Facilitate,
        crypto=crypto,
        recipient=auth_to,
        amount=Decimal(authorization.value).scaleb(-crypto.get_decimals(chain)),
    )


_PAYMENT_COLLECTOR_FACTORY_SELECTOR = (
    "0x" + Web3.keccak(text="deployCollector(bytes32,address,address,uint256)")[:4].hex()
)


def compute_create2_address(
    *,
    factory_address: str,
    salt: bytes,
    init_code_hash: bytes,
) -> str:
    """按 EIP-1014 公式计算 CREATE2 部署后的合约地址。"""
    _require_bytes32("salt", salt)
    _require_bytes32("init_code_hash", init_code_hash)

    factory_checksum = Web3.to_checksum_address(factory_address)
    payload = (
        b"\xff"
        + Web3.to_bytes(hexstr=factory_checksum)
        + bytes(salt)
        + bytes(init_code_hash)
    )
    digest = Web3.keccak(payload)
    return Web3.to_checksum_address(f"0x{digest[12:].hex()}")


def build_payment_collector_deploy_intent(
    *,
    address: Address,
    chain: Chain,
    salt: bytes,
    vault_address: str,
    crypto: Crypto,
    expected_collect_value_raw: int,
    collector_init_code_hash: bytes,
    gas: int,
) -> EvmTxIntent:
    if not chain.create2_factory_address:
        raise ValueError(f"Chain {chain.code} 未配置 create2_factory_address")
    if expected_collect_value_raw < 0:
        raise ValueError("expected_collect_value_raw must be >= 0")

    _require_bytes32("salt", salt)
    _require_bytes32("init_code_hash", collector_init_code_hash)

    token_addr = crypto.address(chain)
    if not token_addr:
        raise ValueError(
            f"Crypto {crypto.symbol} is not deployed on chain {chain.code}"
        )

    factory_address = Web3.to_checksum_address(chain.create2_factory_address)
    vault_checksum = Web3.to_checksum_address(vault_address)
    token_checksum = Web3.to_checksum_address(token_addr)
    collector_address = compute_create2_address(
        factory_address=factory_address,
        salt=salt,
        init_code_hash=collector_init_code_hash,
    )
    encoded_args = eth_abi.encode(
        ["bytes32", "address", "address", "uint256"],
        [
            bytes(salt),
            vault_checksum,
            token_checksum,
            expected_collect_value_raw,
        ],
    ).hex()

    return build_contract_call_intent(
        address=address,
        chain=chain,
        contract_address=factory_address,
        data=f"{_PAYMENT_COLLECTOR_FACTORY_SELECTOR}{encoded_args}",
        gas=gas,
        transfer_type=TransferType.ContractDeployCollect,
        crypto=crypto,
        recipient=collector_address,
        amount=Decimal(expected_collect_value_raw).scaleb(-crypto.get_decimals(chain)),
    )
