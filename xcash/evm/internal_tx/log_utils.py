from __future__ import annotations

from decimal import Decimal
from typing import Any

from web3 import Web3

from evm.scanner.constants import ERC20_TRANSFER_TOPIC0


def matches_transfer_log(
    log: dict,
    *,
    token: str,
    from_address: str,
    to_address: str,
    value: Decimal,
) -> bool:
    """判断单条 receipt log 是否匹配指定 ERC20 Transfer 过滤条件。"""
    address = str(log.get("address") or "")
    if not address:
        return False
    if Web3.to_checksum_address(address) != Web3.to_checksum_address(token):
        return False

    topics = list(log.get("topics") or [])
    if len(topics) < 3:
        return False
    if _hex_lower(topics[0]) != ERC20_TRANSFER_TOPIC0.removeprefix("0x").lower():
        return False

    log_from = Web3.to_checksum_address(f"0x{_hex_lower(topics[1])[-40:]}")
    log_to = Web3.to_checksum_address(f"0x{_hex_lower(topics[2])[-40:]}")
    if log_from != Web3.to_checksum_address(from_address):
        return False
    if log_to != Web3.to_checksum_address(to_address):
        return False

    raw_data = _hex_lower(log.get("data", "0x0"))
    if not raw_data:
        return False
    return Decimal(int(raw_data, 16)) == value


def normalize_log_index(value: Any) -> int:
    """把 logIndex 解析为 int，兼容十进制 / 0x 十六进制 / int。"""
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    return int(value)


def _hex_lower(value: Any) -> str:
    """提取去掉 0x 前缀的小写十六进制串。"""
    raw = value.hex() if hasattr(value, "hex") else str(value)
    return raw.removeprefix("0x").lower()

