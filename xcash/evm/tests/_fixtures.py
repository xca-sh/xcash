"""EVM 内部交易测试常用 fixture 构造器。"""

from __future__ import annotations

from decimal import Decimal

from web3 import Web3

from chains.constants import ChainCode
from chains.models import Address
from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import TxTaskType
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
    chain_id: int | None = None,
    native_coin: Crypto | None = None,
    confirm_block_count: int = 6,
    rpc: str = "",
    latest_block_number: int = 0,
    evm_log_max_block_range: int | None = None,
    active: bool = True,
) -> Chain:
    # Chain 已收窄为 spec 驱动：chain_id / native_coin / confirm_block_count 全部由
    # ChainCode 常量推导，不再是可写字段，这里仅为兼容旧调用签名而保留并忽略。
    _ = (chain_id, native_coin, confirm_block_count)
    chain_code = code if code in ChainCode.values else ChainCode.Anvil
    chain = Chain.objects.create(
        code=chain_code,
        rpc="",
        active=False,
    )
    # rpc / latest_block_number / evm_log_max_block_range 仍是真实字段，但 save() 会对
    # rpc 触发 chain_id 远端校验；测试构造的 rpc 多为占位且 w3 已被 mock，故用 update()
    # 直接落库绕过校验，避免无谓的网络连接。
    updates: dict[str, object] = {}
    if rpc:
        updates["rpc"] = rpc
    if active:
        updates.setdefault("rpc", "http://evm-test.invalid")
        updates["active"] = True
    if latest_block_number:
        updates["latest_block_number"] = latest_block_number
    if evm_log_max_block_range is not None:
        updates["evm_log_max_block_range"] = evm_log_max_block_range
    if updates:
        Chain.objects.filter(pk=chain.pk).update(**updates)
        chain.refresh_from_db()
    return chain


def make_erc20_token(
    *,
    chain: Chain,
    crypto: Crypto | None = None,
    address_suffix: str = "ee",
    decimals: int | None = 6,
) -> Crypto:
    from currencies.models import ChainCryptoDeployment

    crypto = crypto or make_crypto(symbol=f"TKN-{address_suffix}")
    address = Web3.to_checksum_address("0x" + address_suffix.rjust(40, "0"))
    ChainCryptoDeployment.objects.create(
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
    usage: AddressUsage = AddressUsage.HotWallet,
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


def make_tx_task(
    *,
    chain: Chain,
    address: Address,
    tx_type: TxTaskType = TxTaskType.VaultSlotCollect,
    crypto: Crypto | None = None,
    amount: Decimal = Decimal("1.0"),
    recipient_suffix: str = "ff",
    tx_hash_suffix: str = "01",
    status: TxTaskStatus = TxTaskStatus.PENDING_CHAIN,
) -> TxTask:
    return TxTask.objects.create(
        chain=chain,
        sender=address,
        tx_type=tx_type,
        tx_hash=make_tx_hash(tx_hash_suffix),
        status=status,
    )
