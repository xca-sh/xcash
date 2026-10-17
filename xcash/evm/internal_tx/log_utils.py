from __future__ import annotations

from decimal import Decimal

from evm.hexutil import checksum_address
from evm.hexutil import hex_lower
from evm.hexutil import topic_to_address
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
    if checksum_address(address) != checksum_address(token):
        return False

    topics = list(log.get("topics") or [])
    if len(topics) < 3:
        return False
    if hex_lower(topics[0]) != ERC20_TRANSFER_TOPIC0.removeprefix("0x").lower():
        return False

    log_from = topic_to_address(topics[1])
    log_to = topic_to_address(topics[2])
    if log_from != checksum_address(from_address):
        return False
    if log_to != checksum_address(to_address):
        return False

    raw_data = hex_lower(log.get("data", "0x0"))
    if not raw_data:
        return False
    return Decimal(int(raw_data, 16)) == value
