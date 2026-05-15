"""evm/intents 骨架：dataclass + 校验工具 + 派发表 + 闸门。"""

from dataclasses import FrozenInstanceError

import pytest

from chains.models import TransferType
from evm.choices import TxKind
from evm.intents import EvmTxIntent
from evm.intents import _normalize_hex_calldata
from evm.intents import _require_bytes32
from evm.intents import assert_transfer_type_implemented
from evm.intents import get_preflight_buffer_multiplier
from evm.intents import is_gas_rechargeable


@pytest.fixture
def simple_intent():
    return EvmTxIntent(
        address=object(),
        chain=object(),
        tx_kind=TxKind.NATIVE_TRANSFER,
        to="0x" + "a" * 40,
        value=0,
        data="",
        gas=21000,
        transfer_type=TransferType.Withdrawal,
        crypto=None,
        recipient=None,
        amount=None,
    )


def test_evm_tx_intent_is_frozen(simple_intent):
    with pytest.raises(FrozenInstanceError):
        simple_intent.value = 999


def test_normalize_accepts_empty_string_returns_0x():
    assert _normalize_hex_calldata("") == "0x"


def test_normalize_accepts_0x_returns_0x():
    assert _normalize_hex_calldata("0x") == "0x"


def test_normalize_lowercases_and_adds_prefix():
    assert _normalize_hex_calldata("A9059CBB") == "0xa9059cbb"
    assert _normalize_hex_calldata("0xA9059CBB") == "0xa9059cbb"


def test_normalize_rejects_odd_length():
    with pytest.raises(ValueError, match="even-length"):
        _normalize_hex_calldata("0xa")


def test_normalize_rejects_non_hex():
    with pytest.raises(ValueError, match="hex string"):
        _normalize_hex_calldata("zzzz")


def test_require_bytes32_accepts_32_bytes():
    _require_bytes32("nonce", b"\x00" * 32)


def test_require_bytes32_rejects_short():
    with pytest.raises(ValueError, match="nonce"):
        _require_bytes32("nonce", b"\x00" * 31)


def test_require_bytes32_rejects_non_bytes():
    with pytest.raises(ValueError, match="nonce"):
        _require_bytes32("nonce", "x" * 64)


def test_preflight_buffer_multiplier_for_native_and_call():
    assert get_preflight_buffer_multiplier(TxKind.NATIVE_TRANSFER) > 0
    assert get_preflight_buffer_multiplier(TxKind.CONTRACT_CALL) > 0


def test_gas_rechargeable_for_deposit_collection_only():
    assert is_gas_rechargeable(TransferType.DepositCollection) is True
    assert is_gas_rechargeable(TransferType.Withdrawal) is False
    assert is_gas_rechargeable(TransferType.GasRecharge) is False
    assert is_gas_rechargeable(TransferType.Invoice) is False
    assert is_gas_rechargeable(TransferType.Deposit) is False
    assert is_gas_rechargeable(TransferType.X402Facilitate) is False
    assert is_gas_rechargeable(TransferType.ContractDeployCollect) is False


def test_assert_blocks_x402_facilitate():
    with pytest.raises(NotImplementedError, match="§11"):
        assert_transfer_type_implemented(TransferType.X402Facilitate)


def test_assert_blocks_contract_deploy_collect():
    with pytest.raises(NotImplementedError, match="§11"):
        assert_transfer_type_implemented(TransferType.ContractDeployCollect)


def test_assert_allows_legacy_transfer_types():
    for tt in [
        TransferType.Withdrawal,
        TransferType.DepositCollection,
        TransferType.GasRecharge,
        TransferType.Invoice,
        TransferType.Deposit,
    ]:
        assert_transfer_type_implemented(tt)
