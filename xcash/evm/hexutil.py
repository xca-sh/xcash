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
