"""EVM 十六进制、哈希与地址的统一归一化工具。

扫描器、轮询器、内部交易收口都要把 web3 返回的 HexBytes / str 统一成可比较的形式；
这些转换集中在这里，避免各模块各写一份。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from web3 import Web3


def hex_lower(value: Any) -> str:
    """去掉 0x 前缀的小写十六进制串，兼容 bytes / HexBytes / str。"""
    raw = value.hex() if hasattr(value, "hex") else str(value)
    return raw.removeprefix("0x").removeprefix("0X").lower()


@lru_cache(maxsize=65536)
def checksum_address(value: str) -> str:
    """转 EIP-55 checksum 地址；非法地址抛 ValueError。

    checksum 要做一次 keccak（实测约 12µs）。主网一个扫描窗口有数千条代币转账，
    交易所、路由合约等地址反复出现，且同一地址在候选匹配与日志解析里各要转一次，
    按进程缓存后每个地址只算一次。ValueError 不会被缓存。
    """
    return Web3.to_checksum_address(value)


def topic_to_address(topic: Any) -> str:
    """从 32 字节 topic 取后 20 字节作为 checksum 地址；长度不足或非法抛 ValueError。"""
    raw = hex_lower(topic)
    if len(raw) < 40:
        raise ValueError(f"topic too short for address: {topic!r}")
    return checksum_address(f"0x{raw[-40:]}")


def prefixed_hex(value: Any) -> str | None:
    """带 0x 前缀的小写十六进制串（哈希 / topic）；None 或空值返回 None。"""
    if value is None:
        return None
    raw = hex_lower(value)
    return f"0x{raw}" if raw else None


def required_hash(value: Any) -> str:
    """prefixed_hex 的必填变体：空值抛 ValueError。"""
    normalized = prefixed_hex(value)
    if normalized is None:
        raise ValueError("hash is empty")
    return normalized


def parse_int(value: Any) -> int:
    """兼容 int / 0x 十六进制串 / 十进制串的整数解析；空串按 0。"""
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.startswith(("0x", "0X")):
        return int(text, 16)
    return int(text) if text else 0


def parse_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return parse_int(value)
