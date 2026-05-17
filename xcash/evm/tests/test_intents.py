"""evm/intents 骨架：dataclass + 校验工具 + 派发表 + 闸门。"""

import typing
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from decimal import Decimal

import eth_abi
import pytest
from web3 import Web3

import evm.intents as intents_module
from chains.models import OnchainActionType
from evm.choices import TxKind
from evm.intents import Eip3009Authorization
from evm.intents import EvmTxIntent
from evm.intents import X402Authorization
from evm.intents import _normalize_hex_calldata
from evm.intents import _require_bytes32
from evm.intents import assert_action_type_implemented
from evm.intents import build_contract_call_intent
from evm.intents import build_erc20_transfer_intent
from evm.intents import build_native_transfer_intent
from evm.intents import build_payment_collector_deploy_intent
from evm.intents import build_x402_eip3009_facilitate_intent
from evm.intents import compute_create2_address
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
        action_type=OnchainActionType.Withdrawal,
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
    assert is_gas_rechargeable(OnchainActionType.DepositCollection) is True
    assert is_gas_rechargeable(OnchainActionType.Withdrawal) is False
    assert is_gas_rechargeable(OnchainActionType.GasRecharge) is False
    assert is_gas_rechargeable(OnchainActionType.Invoice) is False
    assert is_gas_rechargeable(OnchainActionType.Deposit) is False
    assert is_gas_rechargeable(OnchainActionType.X402Facilitate) is False
    assert is_gas_rechargeable(OnchainActionType.ContractDeployCollect) is False


def test_assert_allows_x402_facilitate_after_lifecycle_is_connected():
    assert_action_type_implemented(OnchainActionType.X402Facilitate)


def test_assert_allows_contract_deploy_collect_after_lifecycle_is_connected():
    assert_action_type_implemented(OnchainActionType.ContractDeployCollect)


def test_assert_allows_legacy_action_types():
    for tt in [
        OnchainActionType.Withdrawal,
        OnchainActionType.DepositCollection,
        OnchainActionType.GasRecharge,
        OnchainActionType.Invoice,
        OnchainActionType.Deposit,
    ]:
        assert_action_type_implemented(tt)


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


def _fake_chain(native_coin=None, create2_factory_address=None):
    class FakeChain:
        def __init__(self):
            self.code = "ETH"
            self.native_coin = native_coin or _fake_crypto(symbol="ETH", decimals=18)
            self.base_transfer_gas = 21000
            self.erc20_transfer_gas = 65000
            self.create2_factory_address = create2_factory_address

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
        action_type=OnchainActionType.Withdrawal,
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
            action_type=OnchainActionType.Withdrawal,
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
        action_type=OnchainActionType.Withdrawal,
    )

    assert intent.tx_kind == TxKind.CONTRACT_CALL
    assert intent.to == Web3.to_checksum_address(token_address)
    assert intent.value == 0
    assert intent.data.startswith("0xa9059cbb")
    decoded_recipient, decoded_value_raw = eth_abi.decode(
        ["address", "uint256"],
        bytes.fromhex(intent.data.removeprefix("0xa9059cbb")),
    )
    assert Web3.to_checksum_address(decoded_recipient) == Web3.to_checksum_address(
        recipient
    )
    assert decoded_value_raw == value_raw
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
            action_type=OnchainActionType.Withdrawal,
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
            action_type=OnchainActionType.Withdrawal,
        )


def test_build_contract_call_intent_sets_basic_fields():
    chain = _fake_chain()
    contract_address = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    recipient = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    intent = build_contract_call_intent(
        address=_fake_address(),
        chain=chain,
        contract_address=contract_address,
        data="A9059CBB",
        gas=50000,
        action_type=OnchainActionType.Invoice,
        value=7,
        recipient=recipient,
    )

    assert intent.tx_kind == TxKind.CONTRACT_CALL
    assert intent.to == Web3.to_checksum_address(contract_address)
    assert intent.data == "0xa9059cbb"
    assert intent.gas == 50000
    assert intent.value == 7
    assert intent.recipient == Web3.to_checksum_address(recipient)


def test_build_contract_call_intent_defaults_value_to_zero():
    intent = build_contract_call_intent(
        address=_fake_address(),
        chain=_fake_chain(),
        contract_address="0x2222222222222222222222222222222222222222",
        data="0x",
        gas=50000,
        action_type=OnchainActionType.Invoice,
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
            action_type=OnchainActionType.Invoice,
        )


def test_build_contract_call_intent_rejects_negative_value():
    with pytest.raises(ValueError, match="value must be >= 0"):
        build_contract_call_intent(
            address=_fake_address(),
            chain=_fake_chain(),
            contract_address="0x2222222222222222222222222222222222222222",
            data="0x",
            gas=50000,
            action_type=OnchainActionType.Invoice,
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
            action_type=OnchainActionType.Invoice,
        )


def _good_auth(**overrides):
    values = {
        "from_address": "0x1111111111111111111111111111111111111111",
        "to": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "value": 1234567,
        "valid_after": 100,
        "valid_before": 200,
        "nonce": b"\x01" * 32,
        "v": 27,
        "r": b"\x02" * 32,
        "s": b"\x03" * 32,
    }
    values.update(overrides)
    return Eip3009Authorization(**values)


def test_eip3009_authorization_is_x402_authorization():
    assert isinstance(_good_auth(), X402Authorization)


def test_build_x402_eip3009_facilitate_intent_sets_contract_call_fields(
    monkeypatch,
):
    monkeypatch.setattr(
        intents_module,
        "get_x402_eip3009_facilitate_gas",
        lambda chain: 200000,
    )
    token_address = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    crypto = _fake_crypto(symbol="USDC", decimals=6, token_address=token_address)
    chain = _fake_chain()
    authorization = _good_auth()

    intent = build_x402_eip3009_facilitate_intent(
        address=_fake_address(),
        chain=chain,
        crypto=crypto,
        authorization=authorization,
    )

    assert intent.tx_kind == TxKind.CONTRACT_CALL
    assert intent.to == Web3.to_checksum_address(token_address)
    assert intent.to != Web3.to_checksum_address(authorization.to)
    assert intent.recipient == Web3.to_checksum_address(authorization.to)
    assert intent.action_type == OnchainActionType.X402Facilitate
    assert intent.gas == 200000
    assert intent.data.startswith("0xe3ee160e")
    assert intent.amount == Decimal(authorization.value).scaleb(-6)

    decoded = eth_abi.decode(
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
        bytes.fromhex(intent.data.removeprefix("0xe3ee160e")),
    )
    assert Web3.to_checksum_address(decoded[0]) == Web3.to_checksum_address(
        authorization.from_address
    )
    assert Web3.to_checksum_address(decoded[1]) == Web3.to_checksum_address(
        authorization.to
    )
    assert decoded[2:] == (
        authorization.value,
        authorization.valid_after,
        authorization.valid_before,
        authorization.nonce,
        authorization.v,
        authorization.r,
        authorization.s,
    )


def test_build_x402_eip3009_facilitate_intent_rejects_negative_value():
    with pytest.raises(ValueError, match=r"authorization\.value"):
        build_x402_eip3009_facilitate_intent(
            address=_fake_address(),
            chain=_fake_chain(),
            crypto=_fake_crypto(token_address="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
            authorization=_good_auth(value=-1),
        )


def test_build_x402_eip3009_facilitate_intent_rejects_invalid_validity_window():
    with pytest.raises(ValueError, match="valid_after"):
        build_x402_eip3009_facilitate_intent(
            address=_fake_address(),
            chain=_fake_chain(),
            crypto=_fake_crypto(token_address="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
            authorization=_good_auth(valid_after=200, valid_before=200),
        )


def test_build_x402_eip3009_facilitate_intent_rejects_invalid_v():
    with pytest.raises(ValueError, match=r"authorization\.v"):
        build_x402_eip3009_facilitate_intent(
            address=_fake_address(),
            chain=_fake_chain(),
            crypto=_fake_crypto(token_address="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
            authorization=_good_auth(v=1),
        )


def test_build_x402_eip3009_facilitate_intent_rejects_invalid_nonce():
    with pytest.raises(ValueError, match=r"authorization\.nonce"):
        build_x402_eip3009_facilitate_intent(
            address=_fake_address(),
            chain=_fake_chain(),
            crypto=_fake_crypto(token_address="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
            authorization=_good_auth(nonce=b"\x01" * 31),
        )


def test_build_x402_eip3009_facilitate_intent_rejects_crypto_not_deployed_on_chain():
    crypto = _fake_crypto(symbol="USDC", token_address=None)
    chain = _fake_chain()

    with pytest.raises(ValueError, match="Crypto USDC is not deployed on chain ETH"):
        build_x402_eip3009_facilitate_intent(
            address=_fake_address(),
            chain=chain,
            crypto=crypto,
            authorization=_good_auth(),
        )


def test_compute_create2_address_matches_eip1014_example():
    address = compute_create2_address(
        factory_address="0x0000000000000000000000000000000000000000",
        salt=b"\x00" * 32,
        init_code_hash=Web3.keccak(b"\x00"),
    )

    assert address.lower() == "0x4d1a2e2bb4f88f0250f26ffff098b0b30b26bf38"


def test_compute_create2_address_requires_keyword_arguments():
    with pytest.raises(TypeError):
        compute_create2_address(
            "0x0000000000000000000000000000000000000000",
            b"\x00" * 32,
            Web3.keccak(b"\x00"),
        )


def test_build_payment_collector_deploy_intent_sets_contract_call_fields():
    factory_address = "0x1111111111111111111111111111111111111111"
    vault_address = "0x2222222222222222222222222222222222222222"
    token_address = "0x3333333333333333333333333333333333333333"
    salt = b"\x01" * 32
    collector_init_code_hash = b"\x02" * 32
    expected_collect_value_raw = 1234567
    gas = 180000
    crypto = _fake_crypto(symbol="USDT", decimals=6, token_address=token_address)
    chain = _fake_chain(create2_factory_address=factory_address)

    intent = build_payment_collector_deploy_intent(
        address=_fake_address(),
        chain=chain,
        salt=salt,
        vault_address=vault_address,
        crypto=crypto,
        expected_collect_value_raw=expected_collect_value_raw,
        collector_init_code_hash=collector_init_code_hash,
        gas=gas,
    )

    expected_collector = compute_create2_address(
        factory_address=factory_address,
        salt=salt,
        init_code_hash=collector_init_code_hash,
    )
    assert intent.tx_kind == TxKind.CONTRACT_CALL
    assert intent.to == Web3.to_checksum_address(factory_address)
    assert intent.recipient == expected_collector
    assert intent.recipient != Web3.to_checksum_address(factory_address)
    assert intent.recipient != Web3.to_checksum_address(vault_address)
    assert intent.action_type == OnchainActionType.ContractDeployCollect
    assert intent.gas == gas
    assert intent.amount == Decimal(expected_collect_value_raw).scaleb(-6)
    assert intent.crypto is crypto
    assert intent.data.startswith("0x")


def test_build_payment_collector_deploy_intent_encodes_factory_call_data():
    factory_address = "0x1111111111111111111111111111111111111111"
    vault_address = "0x2222222222222222222222222222222222222222"
    token_address = "0x3333333333333333333333333333333333333333"
    salt = b"\x01" * 32
    expected_collect_value_raw = 1234567
    crypto = _fake_crypto(symbol="USDT", decimals=6, token_address=token_address)
    chain = _fake_chain(create2_factory_address=factory_address)

    intent = build_payment_collector_deploy_intent(
        address=_fake_address(),
        chain=chain,
        salt=salt,
        vault_address=vault_address,
        crypto=crypto,
        expected_collect_value_raw=expected_collect_value_raw,
        collector_init_code_hash=b"\x02" * 32,
        gas=180000,
    )

    expected_selector = "0x" + Web3.keccak(
        text="deployCollector(bytes32,address,address,uint256)"
    )[:4].hex()
    assert intent.data.startswith(expected_selector)
    decoded = eth_abi.decode(
        ["bytes32", "address", "address", "uint256"],
        bytes.fromhex(intent.data.removeprefix(expected_selector)),
    )
    assert decoded[0] == salt
    assert Web3.to_checksum_address(decoded[1]) == Web3.to_checksum_address(
        vault_address
    )
    assert Web3.to_checksum_address(decoded[2]) == Web3.to_checksum_address(
        token_address
    )
    assert decoded[3] == expected_collect_value_raw


def test_build_payment_collector_deploy_intent_rejects_missing_factory():
    chain = _fake_chain(create2_factory_address=None)

    with pytest.raises(ValueError, match=r"ETH.*create2_factory_address"):
        build_payment_collector_deploy_intent(
            address=_fake_address(),
            chain=chain,
            salt=b"\x01" * 32,
            vault_address="0x2222222222222222222222222222222222222222",
            crypto=_fake_crypto(
                token_address="0x3333333333333333333333333333333333333333"
            ),
            expected_collect_value_raw=1,
            collector_init_code_hash=b"\x02" * 32,
            gas=180000,
        )


def test_build_payment_collector_deploy_intent_rejects_short_salt():
    chain = _fake_chain(
        create2_factory_address="0x1111111111111111111111111111111111111111"
    )

    with pytest.raises(ValueError, match="salt"):
        build_payment_collector_deploy_intent(
            address=_fake_address(),
            chain=chain,
            salt=b"\x01" * 31,
            vault_address="0x2222222222222222222222222222222222222222",
            crypto=_fake_crypto(
                token_address="0x3333333333333333333333333333333333333333"
            ),
            expected_collect_value_raw=1,
            collector_init_code_hash=b"\x02" * 32,
            gas=180000,
        )


def test_build_payment_collector_deploy_intent_rejects_negative_expected_value():
    chain = _fake_chain(
        create2_factory_address="0x1111111111111111111111111111111111111111"
    )

    with pytest.raises(ValueError, match="expected_collect_value_raw"):
        build_payment_collector_deploy_intent(
            address=_fake_address(),
            chain=chain,
            salt=b"\x01" * 32,
            vault_address="0x2222222222222222222222222222222222222222",
            crypto=_fake_crypto(
                token_address="0x3333333333333333333333333333333333333333"
            ),
            expected_collect_value_raw=-1,
            collector_init_code_hash=b"\x02" * 32,
            gas=180000,
        )


def test_build_payment_collector_deploy_intent_rejects_short_init_code_hash():
    chain = _fake_chain(
        create2_factory_address="0x1111111111111111111111111111111111111111"
    )

    with pytest.raises(ValueError, match="init_code_hash"):
        build_payment_collector_deploy_intent(
            address=_fake_address(),
            chain=chain,
            salt=b"\x01" * 32,
            vault_address="0x2222222222222222222222222222222222222222",
            crypto=_fake_crypto(
                token_address="0x3333333333333333333333333333333333333333"
            ),
            expected_collect_value_raw=1,
            collector_init_code_hash=b"\x02" * 31,
            gas=180000,
        )


def test_build_payment_collector_deploy_intent_rejects_crypto_not_deployed():
    chain = _fake_chain(
        create2_factory_address="0x1111111111111111111111111111111111111111"
    )

    with pytest.raises(ValueError, match="Crypto USDT is not deployed on chain ETH"):
        build_payment_collector_deploy_intent(
            address=_fake_address(),
            chain=chain,
            salt=b"\x01" * 32,
            vault_address="0x2222222222222222222222222222222222222222",
            crypto=_fake_crypto(symbol="USDT", token_address=None),
            expected_collect_value_raw=1,
            collector_init_code_hash=b"\x02" * 32,
            gas=180000,
        )
