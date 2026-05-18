"""EVM 内部交易测试常用 fixture 构造器。"""

from __future__ import annotations

from decimal import Decimal

from web3 import Web3

from chains.models import Address
from chains.models import AddressUsage
from chains.models import BroadcastTask
from chains.models import BroadcastTaskResult
from chains.models import BroadcastTaskStage
from chains.models import Chain
from chains.models import ChainType
from chains.models import OnchainActionType
from chains.models import Wallet
from currencies.models import Crypto


def make_crypto(*, symbol: str = "TST", name: str | None = None) -> Crypto:
    return Crypto.objects.create(
        name=name or symbol,
        symbol=symbol,
        coingecko_id=f"cg-{symbol.lower()}",
    )


def make_evm_chain(
    *,
    code: str,
    chain_id: int,
    native_coin: Crypto | None = None,
    confirm_block_count: int = 6,
) -> Chain:
    native = native_coin or make_crypto(symbol=f"NAT-{code}")
    return Chain.objects.create(
        code=code,
        name=code.upper(),
        type=ChainType.EVM,
        chain_id=chain_id,
        rpc=f"http://{code}.local",
        native_coin=native,
        confirm_block_count=confirm_block_count,
        active=True,
    )


def make_erc20_token(
    *,
    chain: Chain,
    crypto: Crypto | None = None,
    address_suffix: str = "ee",
    decimals: int | None = 6,
) -> Crypto:
    from currencies.models import ChainToken

    crypto = crypto or make_crypto(symbol=f"TKN-{address_suffix}")
    address = Web3.to_checksum_address("0x" + address_suffix.rjust(40, "0"))
    ChainToken.objects.create(
        crypto=crypto,
        chain=chain,
        address=address,
        decimals=decimals,
    )
    return crypto


def make_wallet() -> Wallet:
    return Wallet.objects.create()


def make_evm_system_address(
    *,
    wallet: Wallet | None = None,
    suffix: str = "01",
    usage: AddressUsage = AddressUsage.Vault,
    bip44_account: int = 0,
    address_index: int = 0,
) -> Address:
    wallet = wallet or make_wallet()
    raw = "0x" + suffix.rjust(40, "0")
    return Address.objects.create(
        wallet=wallet,
        chain_type=ChainType.EVM,
        usage=usage,
        bip44_account=bip44_account,
        address_index=address_index,
        address=Web3.to_checksum_address(raw),
    )


def make_tx_hash(suffix: str) -> str:
    return "0x" + suffix.rjust(64, "0")


def make_broadcast_task(
    *,
    chain: Chain,
    address: Address,
    action_type: OnchainActionType = OnchainActionType.Withdrawal,
    crypto: Crypto | None = None,
    amount: Decimal = Decimal("1.0"),
    recipient_suffix: str = "ff",
    tx_hash_suffix: str = "01",
    stage: BroadcastTaskStage = BroadcastTaskStage.PENDING_CHAIN,
    result: BroadcastTaskResult = BroadcastTaskResult.UNKNOWN,
) -> BroadcastTask:
    return BroadcastTask.objects.create(
        chain=chain,
        address=address,
        action_type=action_type,
        tx_hash=make_tx_hash(tx_hash_suffix),
        stage=stage,
        result=result,
    )
