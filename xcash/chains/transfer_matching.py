from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from web3 import Web3

from chains.models import Chain
from chains.models import ChainType
from chains.models import OnchainTransfer

if TYPE_CHECKING:
    from currencies.models import Crypto


def addresses_equal(left: str | None, right: str | None, *, chain: Chain) -> bool:
    if not left or not right:
        return False
    if chain.type == ChainType.EVM:
        try:
            return Web3.to_checksum_address(str(left)) == Web3.to_checksum_address(
                str(right)
            )
        except ValueError:
            return False
    return str(left) == str(right)


def raw_amount(*, amount: Decimal, crypto: Crypto, chain: Chain) -> Decimal:
    return Decimal(amount).scaleb(crypto.get_decimals(chain))


def transfer_matches(
    transfer: OnchainTransfer,
    *,
    chain: Chain,
    crypto: Crypto,
    from_address: str,
    to_address: str,
    value: Decimal,
) -> bool:
    if transfer.chain_id != chain.pk:
        return False
    if transfer.crypto_id != crypto.pk:
        return False
    if not addresses_equal(transfer.from_address, from_address, chain=chain):
        return False
    if not addresses_equal(transfer.to_address, to_address, chain=chain):
        return False
    return Decimal(transfer.value) == Decimal(value)
