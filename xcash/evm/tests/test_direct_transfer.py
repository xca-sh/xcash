from decimal import Decimal

import eth_abi
import pytest
from web3 import Web3

from chains.models import OnchainActionType
from currencies.models import ChainToken
from evm.choices import TxKind
from evm.internal_tx.direct_transfer import decode_direct_transfer_fields
from evm.models import EvmBroadcastTask
from evm.tests._fixtures import make_broadcast_task
from evm.tests._fixtures import make_crypto
from evm.tests._fixtures import make_erc20_token
from evm.tests._fixtures import make_evm_chain
from evm.tests._fixtures import make_evm_system_address

_TRANSFER_SELECTOR = "0xa9059cbb"
_TRANSFER_FROM_SELECTOR = "0x23b872dd"


def _transfer_calldata(*, to: str, value: int, selector: str = _TRANSFER_SELECTOR) -> str:
    return selector + eth_abi.encode(["address", "uint256"], [to, value]).hex()


def _make_evm_task(
    *,
    chain,
    address,
    to: str,
    data: str,
    nonce: int = 0,
    tx_kind: str = TxKind.CONTRACT_CALL,
):
    base_task = make_broadcast_task(
        chain=chain,
        address=address,
        action_type=OnchainActionType.Withdrawal,
        tx_hash_suffix=f"{nonce + 1:02x}",
    )
    return EvmBroadcastTask.objects.create(
        base_task=base_task,
        address=address,
        chain=chain,
        nonce=nonce,
        to=to,
        value=0,
        data=data,
        gas=60_000,
        tx_kind=tx_kind,
        gas_price=1,
        signed_payload="0x01",
    )


@pytest.mark.django_db
def test_decode_standard_erc20_transfer_fields_uses_case_insensitive_token_lookup():
    native = make_crypto(symbol="NDT")
    chain = make_evm_chain(code="direct-transfer", chain_id=910001, native_coin=native)
    address = make_evm_system_address(suffix="01")
    crypto = make_crypto(symbol="DUSDT")
    token_address = Web3.to_checksum_address(
        "0x0000000000000000000000000000000000000abc"
    )
    ChainToken.objects.create(
        crypto=crypto,
        chain=chain,
        address=token_address.lower(),
        decimals=6,
    )
    recipient = Web3.to_checksum_address(
        "0x0000000000000000000000000000000000000def"
    )
    evm_task = _make_evm_task(
        chain=chain,
        address=address,
        to=token_address,
        data=_transfer_calldata(to=recipient, value=1_234_567),
    )

    fields = decode_direct_transfer_fields(chain=chain, broadcast_task=evm_task.base_task)

    assert fields is not None
    assert fields.crypto == crypto
    assert fields.to_address == recipient
    assert fields.value == Decimal("1234567")
    assert fields.amount == Decimal("1.234567")


@pytest.mark.django_db
def test_decode_returns_none_for_unknown_token():
    native = make_crypto(symbol="NDU")
    chain = make_evm_chain(code="direct-unknown", chain_id=910002, native_coin=native)
    address = make_evm_system_address(suffix="02")
    token_address = Web3.to_checksum_address(
        "0x0000000000000000000000000000000000000abc"
    )
    recipient = Web3.to_checksum_address(
        "0x0000000000000000000000000000000000000def"
    )
    evm_task = _make_evm_task(
        chain=chain,
        address=address,
        to=token_address,
        data=_transfer_calldata(to=recipient, value=1),
    )

    assert decode_direct_transfer_fields(
        chain=chain, broadcast_task=evm_task.base_task
    ) is None


@pytest.mark.django_db
def test_decode_returns_none_for_transfer_from_selector():
    native = make_crypto(symbol="NDF")
    chain = make_evm_chain(code="direct-transfer-from", chain_id=910003, native_coin=native)
    address = make_evm_system_address(suffix="03")
    crypto = make_erc20_token(chain=chain, address_suffix="abc", decimals=6)
    token_address = crypto.address(chain)
    recipient = Web3.to_checksum_address(
        "0x0000000000000000000000000000000000000def"
    )
    evm_task = _make_evm_task(
        chain=chain,
        address=address,
        to=token_address,
        data=_transfer_calldata(
            to=recipient,
            value=1,
            selector=_TRANSFER_FROM_SELECTOR,
        ),
    )

    assert decode_direct_transfer_fields(
        chain=chain, broadcast_task=evm_task.base_task
    ) is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("bad_data", "suffix", "chain_id"),
    [
        ("0xa9059cbb1", "11", 910011),
        ("0xa9059cbbzz", "12", 910012),
        ("0xa9059cbb1234", "13", 910013),
    ],
)
def test_decode_raises_for_malformed_standard_transfer_calldata(
    bad_data, suffix, chain_id
):
    native = make_crypto(symbol=f"NDM{suffix}")
    chain = make_evm_chain(
        code=f"direct-malformed-{suffix}",
        chain_id=chain_id,
        native_coin=native,
    )
    address = make_evm_system_address(suffix=suffix)
    crypto = make_erc20_token(chain=chain, address_suffix=f"a{suffix}", decimals=6)
    evm_task = _make_evm_task(
        chain=chain,
        address=address,
        to=crypto.address(chain),
        data=bad_data,
    )

    with pytest.raises(ValueError, match="invalid ERC20 transfer calldata"):
        decode_direct_transfer_fields(chain=chain, broadcast_task=evm_task.base_task)
