"""x402 gas 常量表与读取函数：行为契约（未配置必须显式报错）。"""
from types import SimpleNamespace

import pytest

from evm.constants import get_x402_eip3009_facilitate_gas


def test_returns_configured_value_for_known_chain_id():
    # 已配置 chain_id（Ethereum mainnet）应返回 dict 内的整数
    chain = SimpleNamespace(chain_id=1, code="ethereum")
    assert get_x402_eip3009_facilitate_gas(chain) > 0


def test_missing_chain_id_raises_with_helpful_message():
    chain = SimpleNamespace(chain_id=999_999, code="unknown-test")
    with pytest.raises(ValueError) as exc:
        get_x402_eip3009_facilitate_gas(chain)
    msg = str(exc.value)
    assert "unknown-test" in msg
    assert "999999" in msg
    assert "evm/constants.py" in msg  # 错误消息要指引修复位置
