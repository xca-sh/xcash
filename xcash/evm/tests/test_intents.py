"""evm/intents 骨架：dataclass + 校验工具 + 派发表 + 闸门。"""

import typing
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest
from web3 import Web3

import evm.intents as intents_module
from chains.models import TransferType
from evm.choices import TxKind
from evm.intents import EvmTxIntent
from evm.intents import _normalize_hex_calldata
from evm.intents import _require_bytes32
from evm.intents import assert_transfer_type_implemented
from evm.intents import build_contract_call_intent
from evm.intents import build_erc20_transfer_intent
from evm.intents import build_native_transfer_intent
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


def test_evm_tx_intent_amount_annotation_is_decimal():
    hints = typing.get_type_hints(
        EvmTxIntent,
        globalns={
            **vars(intents_module),
            "Address": object,
            "Callable": Callable,
            "Chain": object,
            "Crypto": object,
            "Decimal": Decimal,
        },
    )

    assert hints["amount"] == Decimal | None


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


def _fake_crypto(symbol="USDT", decimals=6, token_address=None):
    class FakeCrypto:
        def __init__(self):
            self.symbol = symbol
            self.decimals = decimals

        def address(self, chain):
            return token_address

        def get_decimals(self, chain):
            return decimals

    return FakeCrypto()


def _fake_chain(native_coin=None):
    class FakeChain:
        def __init__(self):
            self.code = "ETH"
            self.native_coin = native_coin or _fake_crypto(symbol="ETH", decimals=18)
            self.base_transfer_gas = 21000
            self.erc20_transfer_gas = 65000

    return FakeChain()


def _fake_address():
    return object()


def test_build_native_transfer_intent_sets_basic_fields():
    native_coin = _fake_crypto(symbol="ETH", decimals=18)
    chain = _fake_chain(native_coin=native_coin)
    recipient = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    value = 1234567890000000000

    intent = build_native_transfer_intent(
        address=_fake_address(),
        chain=chain,
        to=recipient,
        value=value,
        transfer_type=TransferType.Withdrawal,
    )

    assert intent.tx_kind == TxKind.NATIVE_TRANSFER
    assert intent.to == Web3.to_checksum_address(recipient)
    assert intent.recipient == Web3.to_checksum_address(recipient)
    assert intent.value == value
    assert intent.data == ""
    assert intent.gas == chain.base_transfer_gas
    assert intent.amount == Decimal(value).scaleb(-18)
    assert intent.crypto is native_coin


def test_build_native_transfer_intent_rejects_negative_value():
    with pytest.raises(ValueError, match="value must be >= 0"):
        build_native_transfer_intent(
            address=_fake_address(),
            chain=_fake_chain(),
            to="0x1111111111111111111111111111111111111111",
            value=-1,
            transfer_type=TransferType.Withdrawal,
        )


def test_build_erc20_transfer_intent_sets_basic_fields():
    token_address = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    recipient = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    crypto = _fake_crypto(symbol="USDT", decimals=6, token_address=token_address)
    chain = _fake_chain()
    value_raw = 1234567

    intent = build_erc20_transfer_intent(
        address=_fake_address(),
        chain=chain,
        crypto=crypto,
        to=recipient,
        value_raw=value_raw,
        transfer_type=TransferType.Withdrawal,
    )

    assert intent.tx_kind == TxKind.CONTRACT_CALL
    assert intent.to == Web3.to_checksum_address(token_address)
    assert intent.value == 0
    assert intent.data.startswith("0xa9059cbb")
    assert intent.gas == chain.erc20_transfer_gas
    assert intent.recipient == Web3.to_checksum_address(recipient)
    assert intent.amount == Decimal(value_raw).scaleb(-6)
    assert intent.crypto is crypto


def test_build_erc20_transfer_intent_rejects_negative_value_raw():
    crypto = _fake_crypto(token_address="0x2222222222222222222222222222222222222222")

    with pytest.raises(ValueError, match="value_raw must be >= 0"):
        build_erc20_transfer_intent(
            address=_fake_address(),
            chain=_fake_chain(),
            crypto=crypto,
            to="0x3333333333333333333333333333333333333333",
            value_raw=-1,
            transfer_type=TransferType.Withdrawal,
        )


def test_build_erc20_transfer_intent_rejects_crypto_not_deployed_on_chain():
    crypto = _fake_crypto(symbol="USDC", token_address=None)
    chain = _fake_chain()

    with pytest.raises(ValueError, match="Crypto USDC is not deployed on chain ETH"):
        build_erc20_transfer_intent(
            address=_fake_address(),
            chain=chain,
            crypto=crypto,
            to="0x3333333333333333333333333333333333333333",
            value_raw=1,
            transfer_type=TransferType.Withdrawal,
        )


def test_build_contract_call_intent_sets_basic_fields():
    chain = _fake_chain()
    contract_address = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    intent = build_contract_call_intent(
        address=_fake_address(),
        chain=chain,
        contract_address=contract_address,
        data="A9059CBB",
        gas=50000,
        transfer_type=TransferType.Invoice,
        value=7,
    )

    assert intent.tx_kind == TxKind.CONTRACT_CALL
    assert intent.to == Web3.to_checksum_address(contract_address)
    assert intent.data == "0xa9059cbb"
    assert intent.gas == 50000
    assert intent.value == 7


def test_build_contract_call_intent_defaults_value_to_zero():
    intent = build_contract_call_intent(
        address=_fake_address(),
        chain=_fake_chain(),
        contract_address="0x2222222222222222222222222222222222222222",
        data="0x",
        gas=50000,
        transfer_type=TransferType.Invoice,
    )

    assert intent.value == 0


def test_build_contract_call_intent_rejects_non_positive_gas():
    with pytest.raises(ValueError, match="gas must be > 0"):
        build_contract_call_intent(
            address=_fake_address(),
            chain=_fake_chain(),
            contract_address="0x2222222222222222222222222222222222222222",
            data="0x",
            gas=0,
            transfer_type=TransferType.Invoice,
        )


def test_build_contract_call_intent_rejects_negative_value():
    with pytest.raises(ValueError, match="value must be >= 0"):
        build_contract_call_intent(
            address=_fake_address(),
            chain=_fake_chain(),
            contract_address="0x2222222222222222222222222222222222222222",
            data="0x",
            gas=50000,
            transfer_type=TransferType.Invoice,
            value=-1,
        )


def test_build_contract_call_intent_rejects_non_hex_data():
    with pytest.raises(ValueError, match="hex string"):
        build_contract_call_intent(
            address=_fake_address(),
            chain=_fake_chain(),
            contract_address="0x2222222222222222222222222222222222222222",
            data="zzzz",
            gas=50000,
            transfer_type=TransferType.Invoice,
        )
