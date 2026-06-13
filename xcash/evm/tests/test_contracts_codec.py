"""evm.contracts_codec 的 XcashVaultSlot init_code 与地址预测。"""

import json
from pathlib import Path

import pytest
from eth_utils import is_checksum_address

import evm.contracts_codec as codec
from evm.constants import XCASH_VAULT_SLOT_FACTORY_ADDRESS
from evm.constants import XCASH_VAULT_SLOT_IMPLEMENTATION_ADDRESS

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "xcash_vault_slot_fixtures.json"


@pytest.fixture(scope="module")
def fixtures():
    return json.loads(FIXTURES_PATH.read_text())


def _hex_to_bytes(value: str) -> bytes:
    return bytes.fromhex(value[2:] if value.startswith("0x") else value)


def test_build_xcash_vault_slot_init_code_matches_foundry_fixture(fixtures):
    case = fixtures["xcash_vault_slot"]
    got = codec.build_xcash_vault_slot_init_code(
        vault_slot_implementation=case["vault_slot_implementation"],
        vault=case["vault"],
    )
    assert got == _hex_to_bytes(case["slot_init_code"])


def test_predict_xcash_vault_slot_address_matches_foundry_fixture(fixtures):
    case = fixtures["xcash_vault_slot"]
    got = codec.predict_xcash_vault_slot_address(
        factory=case["factory"],
        vault_slot_implementation=case["vault_slot_implementation"],
        vault=case["vault"],
        salt=_hex_to_bytes(fixtures["salt"]),
    )
    assert got.lower() == case["predicted"].lower()


def test_predict_xcash_vault_slot_address_uses_default_deployment_constants(fixtures):
    case = fixtures["xcash_vault_slot"]
    expected = codec.predict_xcash_vault_slot_address(
        factory=XCASH_VAULT_SLOT_FACTORY_ADDRESS,
        vault_slot_implementation=XCASH_VAULT_SLOT_IMPLEMENTATION_ADDRESS,
        vault=case["vault"],
        salt=_hex_to_bytes(fixtures["salt"]),
    )

    got = codec.predict_xcash_vault_slot_address(
        vault=case["vault"],
        salt=_hex_to_bytes(fixtures["salt"]),
    )

    assert got == expected


def test_predict_xcash_vault_slot_address_changes_with_vault(fixtures):
    first = fixtures["xcash_vault_slot"]
    second = fixtures["xcash_vault_slot_second_vault"]

    assert first["predicted"].lower() != second["predicted"].lower()
    assert codec.predict_xcash_vault_slot_address(
        factory=second["factory"],
        vault_slot_implementation=second["vault_slot_implementation"],
        vault=second["vault"],
        salt=_hex_to_bytes(fixtures["salt"]),
    ).lower() == second["predicted"].lower()


def test_predict_xcash_vault_slot_address_returns_checksum(fixtures):
    case = fixtures["xcash_vault_slot"]
    addr = codec.predict_xcash_vault_slot_address(
        factory=case["factory"],
        vault_slot_implementation=case["vault_slot_implementation"],
        vault=case["vault"],
        salt=_hex_to_bytes(fixtures["salt"]),
    )
    assert is_checksum_address(addr)


def test_predict_xcash_vault_slot_address_rejects_zero_vault(fixtures):
    case = fixtures["xcash_vault_slot"]
    with pytest.raises(ValueError, match="vault address must not be zero"):
        codec.predict_xcash_vault_slot_address(
            factory=case["factory"],
            vault_slot_implementation=case["vault_slot_implementation"],
            vault="0x0000000000000000000000000000000000000000",
            salt=_hex_to_bytes(fixtures["salt"]),
        )


def test_predict_xcash_vault_slot_address_rejects_zero_implementation(fixtures):
    case = fixtures["xcash_vault_slot"]
    with pytest.raises(ValueError, match="vault_slot_implementation address must not be zero"):
        codec.predict_xcash_vault_slot_address(
            factory=case["factory"],
            vault_slot_implementation="0x0000000000000000000000000000000000000000",
            vault=case["vault"],
            salt=_hex_to_bytes(fixtures["salt"]),
        )


def test_predict_xcash_vault_slot_address_requires_32_byte_salt(fixtures):
    case = fixtures["xcash_vault_slot"]
    with pytest.raises(ValueError, match="salt must be 32 bytes"):
        codec.predict_xcash_vault_slot_address(
            factory=case["factory"],
            vault_slot_implementation=case["vault_slot_implementation"],
            vault=case["vault"],
            salt=b"\x00" * 31,
        )
