"""evm.contracts_codec 的 XcashDeposit slot init_code 与地址预测。"""

import json
from pathlib import Path

import pytest
from eth_utils import is_checksum_address

import evm.contracts_codec as codec
from evm.constants import XCASH_DEPOSIT_FACTORY_ADDRESS
from evm.constants import XCASH_DEPOSIT_TEMPLATE_ADDRESS

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "xcash_deposit_slot_fixtures.json"


@pytest.fixture(scope="module")
def fixtures():
    return json.loads(FIXTURES_PATH.read_text())


def _hex_to_bytes(value: str) -> bytes:
    return bytes.fromhex(value[2:] if value.startswith("0x") else value)


def test_build_xcash_deposit_slot_init_code_matches_foundry_fixture(fixtures):
    case = fixtures["xcash_deposit_slot"]
    got = codec.build_xcash_deposit_slot_init_code(
        deposit_template=case["deposit_template"],
        vault=case["vault"],
    )
    assert got == _hex_to_bytes(case["slot_init_code"])


def test_predict_xcash_deposit_slot_address_matches_foundry_fixture(fixtures):
    case = fixtures["xcash_deposit_slot"]
    got = codec.predict_xcash_deposit_slot_address(
        factory=case["factory"],
        deposit_template=case["deposit_template"],
        vault=case["vault"],
        salt=_hex_to_bytes(fixtures["salt"]),
    )
    assert got.lower() == case["predicted"].lower()


def test_predict_xcash_deposit_slot_address_uses_default_deployment_constants(fixtures):
    case = fixtures["xcash_deposit_slot"]
    expected = codec.predict_xcash_deposit_slot_address(
        factory=XCASH_DEPOSIT_FACTORY_ADDRESS,
        deposit_template=XCASH_DEPOSIT_TEMPLATE_ADDRESS,
        vault=case["vault"],
        salt=_hex_to_bytes(fixtures["salt"]),
    )

    got = codec.predict_xcash_deposit_slot_address(
        vault=case["vault"],
        salt=_hex_to_bytes(fixtures["salt"]),
    )

    assert got == expected


def test_predict_xcash_deposit_slot_address_changes_with_vault(fixtures):
    first = fixtures["xcash_deposit_slot"]
    second = fixtures["xcash_deposit_slot_second_vault"]

    assert first["predicted"].lower() != second["predicted"].lower()
    assert codec.predict_xcash_deposit_slot_address(
        factory=second["factory"],
        deposit_template=second["deposit_template"],
        vault=second["vault"],
        salt=_hex_to_bytes(fixtures["salt"]),
    ).lower() == second["predicted"].lower()


def test_predict_xcash_deposit_slot_address_returns_checksum(fixtures):
    case = fixtures["xcash_deposit_slot"]
    addr = codec.predict_xcash_deposit_slot_address(
        factory=case["factory"],
        deposit_template=case["deposit_template"],
        vault=case["vault"],
        salt=_hex_to_bytes(fixtures["salt"]),
    )
    assert is_checksum_address(addr)


def test_predict_xcash_deposit_slot_address_rejects_zero_vault(fixtures):
    case = fixtures["xcash_deposit_slot"]
    with pytest.raises(ValueError, match="vault address must not be zero"):
        codec.predict_xcash_deposit_slot_address(
            factory=case["factory"],
            deposit_template=case["deposit_template"],
            vault="0x0000000000000000000000000000000000000000",
            salt=_hex_to_bytes(fixtures["salt"]),
        )


def test_predict_xcash_deposit_slot_address_rejects_zero_template(fixtures):
    case = fixtures["xcash_deposit_slot"]
    with pytest.raises(ValueError, match="deposit_template address must not be zero"):
        codec.predict_xcash_deposit_slot_address(
            factory=case["factory"],
            deposit_template="0x0000000000000000000000000000000000000000",
            vault=case["vault"],
            salt=_hex_to_bytes(fixtures["salt"]),
        )


def test_predict_xcash_deposit_slot_address_requires_32_byte_salt(fixtures):
    case = fixtures["xcash_deposit_slot"]
    with pytest.raises(ValueError, match="salt must be 32 bytes"):
        codec.predict_xcash_deposit_slot_address(
            factory=case["factory"],
            deposit_template=case["deposit_template"],
            vault=case["vault"],
            salt=b"\x00" * 31,
        )
