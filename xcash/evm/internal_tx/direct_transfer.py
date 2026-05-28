from __future__ import annotations

import binascii
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING
from typing import Any

import eth_abi
from eth_abi.exceptions import DecodingError
from web3 import Web3

from currencies.models import ChainToken
from evm.choices import TxKind
from evm.internal_tx.log_utils import matches_transfer_log
from evm.internal_tx.routing import MatchedTransferFact

if TYPE_CHECKING:
    from chains.models import Chain
    from chains.models import TxTask
    from currencies.models import Crypto


ERC20_TRANSFER_SELECTOR = "0xa9059cbb"


@dataclass(frozen=True)
class DirectTransferFields:
    crypto: Crypto
    to_address: str
    value: Decimal
    amount: Decimal


def _normalize_calldata(data: str) -> str:
    """归一化 calldata：空值统一为 '0x'，其它转小写并补前缀。"""
    if not data or data == "0x":
        return "0x"
    return data.lower() if data.startswith("0x") else f"0x{data.lower()}"


def _normalize_tx_input(value: Any) -> str:
    """归一化 tx.input，兼容 bytes / str / 空值，输出带 0x 的小写串。"""
    if value in (None, "", "0x", "0X"):
        return "0x"
    if isinstance(value, (bytes, bytearray)):
        return f"0x{bytes(value).hex()}"
    if hasattr(value, "hex") and not isinstance(value, str):
        raw = value.hex()
    else:
        raw = str(value)
    if not raw or raw in {"0x", "0X"}:
        return "0x"
    return raw.lower() if raw.startswith(("0x", "0X")) else f"0x{raw.lower()}"


def _decimal_from_tx_value(value: Any) -> Decimal | None:
    """把 tx.value 解析为 Decimal，非法值返回 None。"""
    if value is None:
        return None
    try:
        if isinstance(value, str):
            raw = value.strip()
            return Decimal(int(raw, 16) if raw.startswith(("0x", "0X")) else int(raw))
        if isinstance(value, (bytes, bytearray)):
            return Decimal(int.from_bytes(value, byteorder="big"))
        return Decimal(int(value))
    except (TypeError, ValueError):
        return None


def _native_tx_matches_expected(
    *,
    tx: dict | None,
    from_address: str,
    to_address: str,
    value: Decimal,
) -> bool:
    """校验链上 tx 的 from/to/value/input 与预期一致，且 input 为空（纯转账）。"""
    if tx is None or "input" not in tx:
        return False

    try:
        actual_from = Web3.to_checksum_address(tx.get("from"))
        actual_to = Web3.to_checksum_address(tx.get("to"))
    except (TypeError, ValueError):
        return False

    return (
        actual_from == from_address
        and actual_to == to_address
        and _decimal_from_tx_value(tx.get("value")) == value
        and _normalize_tx_input(tx.get("input")) == "0x"
    )


def _crypto_for_token(*, chain: Chain, token_address: str) -> Crypto | None:
    """按 ERC20 合约地址查到对应 Crypto；未登记返回 None。"""
    token_checksum = Web3.to_checksum_address(token_address)
    chain_token = (
        ChainToken.objects.select_related("crypto")
        .filter(chain=chain, address__iexact=token_checksum)
        .first()
    )
    return chain_token.crypto if chain_token else None


def decode_direct_transfer_fields(
    *,
    chain: Chain,
    tx_task: TxTask,
) -> DirectTransferFields | None:  # noqa: PLR0911
    """从 EVM 执行任务本身解码直接 native/ERC20 transfer 的资产字段。

    这里仅支持本系统内部 builder 产出的标准 ERC-20 transfer(address,uint256)，
    不是通用 calldata 解析器；transferFrom、Permit2、多接收人合约等应走各自
    独立的业务 matcher。
    """
    try:
        evm_task = tx_task.evm_task
    except AttributeError:
        return None

    if evm_task.tx_kind == TxKind.NATIVE_TRANSFER:
        value = Decimal(evm_task.value)
        return DirectTransferFields(
            crypto=chain.native_coin,
            to_address=Web3.to_checksum_address(evm_task.to),
            value=value,
            amount=value.scaleb(-chain.native_coin.get_decimals(chain)),
        )

    data = _normalize_calldata(evm_task.data)
    if evm_task.tx_kind != TxKind.CONTRACT_CALL:
        return None
    if not data.startswith(ERC20_TRANSFER_SELECTOR):
        return None

    crypto = _crypto_for_token(chain=chain, token_address=evm_task.to)
    if crypto is None:
        return None

    try:
        recipient, value_raw = eth_abi.decode(
            ["address", "uint256"],
            Web3.to_bytes(hexstr=f"0x{data[10:]}"),
        )
    except (ValueError, binascii.Error, DecodingError) as exc:
        raise ValueError("invalid ERC20 transfer calldata") from exc

    value = Decimal(value_raw)
    return DirectTransferFields(
        crypto=crypto,
        to_address=Web3.to_checksum_address(recipient),
        value=value,
        amount=value.scaleb(-crypto.get_decimals(chain)),
    )


def match_direct_transfer_fact(
    *,
    chain: Chain,
    tx_task: TxTask,
    receipt: dict,
    tx: dict | None = None,
) -> MatchedTransferFact | None:
    """从 receipt 校验直接转账事实是否与 TxTask 预期一致。

    原生币要求 tx 字段四要素吻合；ERC20 要求 receipt 中恰好一条匹配的 Transfer 日志。
    """
    fields = decode_direct_transfer_fields(chain=chain, tx_task=tx_task)
    if fields is None:
        return None

    from_address = Web3.to_checksum_address(tx_task.address.address)
    if fields.crypto == chain.native_coin:
        if not _native_tx_matches_expected(
            tx=tx,
            from_address=from_address,
            to_address=fields.to_address,
            value=fields.value,
        ):
            return None
        return MatchedTransferFact(
            from_address=from_address,
            to_address=fields.to_address,
            crypto=fields.crypto,
            value=fields.value,
            amount=fields.amount,
        )

    token_addr = fields.crypto.address(chain)
    matches = [
        log
        for log in receipt.get("logs") or []
        if matches_transfer_log(
            log,
            token=token_addr,
            from_address=from_address,
            to_address=fields.to_address,
            value=fields.value,
        )
    ]
    if len(matches) != 1:
        return None

    return MatchedTransferFact(
        from_address=from_address,
        to_address=fields.to_address,
        crypto=fields.crypto,
        value=fields.value,
        amount=fields.amount,
    )
