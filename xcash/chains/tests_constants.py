import pytest

from chains.constants import CHAIN_SPECS
from chains.constants import ChainCode
from chains.constants import ChainType


def test_every_chain_code_has_spec():
    for code in ChainCode:
        assert code.value in CHAIN_SPECS, f"{code} 缺少 ChainSpec"


def test_evm_specs_have_chain_id_and_is_poa():
    for code, spec in CHAIN_SPECS.items():
        if spec.type == ChainType.EVM:
            assert spec.chain_id is not None, f"{code} EVM 链必须有 chain_id"
            assert spec.is_poa is not None, f"{code} EVM 链必须有 is_poa"


def test_tron_spec_has_no_evm_fields():
    spec = CHAIN_SPECS[ChainCode.Tron]
    assert spec.type == ChainType.TRON
    assert spec.chain_id is None
    assert spec.is_poa is None


@pytest.mark.parametrize(
    ("code", "expected_chain_id"),
    [
        (ChainCode.Ethereum, 1),
        (ChainCode.BSC, 56),
        (ChainCode.Polygon, 137),
        (ChainCode.ArbitrumOne, 42161),
        (ChainCode.Optimism, 10),
        (ChainCode.Base, 8453),
        (ChainCode.Avalanche, 43114),
        (ChainCode.Linea, 59144),
        (ChainCode.Scroll, 534352),
    ],
)
def test_evm_chain_ids(code, expected_chain_id):
    assert CHAIN_SPECS[code].chain_id == expected_chain_id


def test_poa_chains():
    poa_codes = {n for n, s in CHAIN_SPECS.items() if s.is_poa}
    assert poa_codes == {ChainCode.BSC, ChainCode.Polygon}


def test_native_coin_symbols():
    assert CHAIN_SPECS[ChainCode.Ethereum].native_coin_symbol == "ETH"
    assert CHAIN_SPECS[ChainCode.BSC].native_coin_symbol == "BNB"
    assert CHAIN_SPECS[ChainCode.Polygon].native_coin_symbol == "POL"
    assert CHAIN_SPECS[ChainCode.Avalanche].native_coin_symbol == "AVAX"
    assert CHAIN_SPECS[ChainCode.Tron].native_coin_symbol == "TRX"
    assert CHAIN_SPECS[ChainCode.Tron].native_coin_decimals == 6


def test_chain_code_groups():
    from chains.constants import EVM_CHAIN_CODES, TRON_CHAIN_CODES

    assert len(EVM_CHAIN_CODES) == sum(
        1 for spec in CHAIN_SPECS.values() if spec.type == ChainType.EVM
    )
    assert TRON_CHAIN_CODES == (ChainCode.Tron,)
    assert ChainCode.Ethereum in EVM_CHAIN_CODES
    assert ChainCode.Tron not in EVM_CHAIN_CODES
