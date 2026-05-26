from __future__ import annotations

from dataclasses import dataclass
from itertools import chain as iter_chain

from django.core.cache import cache
from web3 import Web3

from chains.models import Chain
from chains.models import ChainType
from currencies.models import ChainToken
from evm.models import DepositSlot
from invoices.models import InvoiceBillingMode
from invoices.models import InvoicePaySlot
from invoices.models import InvoicePaySlotStatus
from invoices.models import InvoiceStatus


@dataclass(frozen=True)
class EvmWatchSet:
    """描述某条 EVM 链当前需要关注的地址和代币集合。"""

    watched_addresses: frozenset[str]
    tokens_by_address: dict[str, ChainToken]


EVM_WATCHED_ADDRESSES_CACHE_KEY = "evm:scanner:watched_addresses"
EVM_CHAIN_TOKENS_CACHE_KEY_TEMPLATE = "evm:scanner:chain_tokens:{chain_id}"
EVM_WATCH_SET_CACHE_TIMEOUT = None
EVM_WATCH_SET_ITERATOR_CHUNK_SIZE = 1_000


def _normalize_address(address: str) -> str:
    # 扫描器统一将地址标准化为 checksum，保证 DB 数据与 RPC 返回值可直接比对。
    return Web3.to_checksum_address(str(address))


def load_watch_set(*, chain: Chain, refresh: bool = False) -> EvmWatchSet:
    """加载某条链上需要监听的 DepositSlot 地址与受支持 ERC20 合约集合。"""

    if refresh:
        watched_addresses = refresh_evm_watched_addresses()
        tokens_by_address = refresh_evm_chain_tokens(chain=chain)
        return EvmWatchSet(
            watched_addresses=watched_addresses,
            tokens_by_address=tokens_by_address,
        )

    watched_addresses = cache.get(EVM_WATCHED_ADDRESSES_CACHE_KEY)
    if watched_addresses is None:
        watched_addresses = refresh_evm_watched_addresses()

    chain_tokens_cache_key = _chain_tokens_cache_key(chain=chain)
    tokens_by_address = cache.get(chain_tokens_cache_key)
    if tokens_by_address is None:
        tokens_by_address = refresh_evm_chain_tokens(chain=chain)

    return EvmWatchSet(
        watched_addresses=watched_addresses,
        tokens_by_address=tokens_by_address,
    )


def refresh_evm_watched_addresses() -> frozenset[str]:
    """重建 EVM 全局观察地址缓存。

    DepositSlot 和 contract InvoicePaySlot 是当前 EVM 入账观察面。
    系统 Address 只作为部署、归集、提现等内部交易的发起账户，不进入 scanner。
    """

    watched_addresses = _load_evm_watched_addresses_from_db()
    cache.set(
        EVM_WATCHED_ADDRESSES_CACHE_KEY,
        watched_addresses,
        timeout=EVM_WATCH_SET_CACHE_TIMEOUT,
    )
    return watched_addresses


def refresh_evm_chain_tokens(*, chain: Chain) -> dict[str, ChainToken]:
    """重建指定 EVM 链的 ERC20 合约缓存。"""

    tokens_by_address = _load_evm_chain_tokens_from_db(chain=chain)
    cache.set(
        _chain_tokens_cache_key(chain=chain),
        tokens_by_address,
        timeout=EVM_WATCH_SET_CACHE_TIMEOUT,
    )
    return tokens_by_address


def clear_evm_watch_set_cache(*, chain: Chain | None = None) -> None:
    """清空 EVM 观察集缓存，主要用于测试和运维脚本。"""

    clear_evm_watched_addresses_cache()
    if chain is not None:
        clear_evm_chain_tokens_cache(chain=chain)
        return
    delete_pattern = getattr(cache, "delete_pattern", None)
    if callable(delete_pattern):
        delete_pattern(EVM_CHAIN_TOKENS_CACHE_KEY_TEMPLATE.format(chain_id="*"))


def clear_evm_watched_addresses_cache() -> None:
    """清空 EVM 全局观察地址缓存。"""

    cache.delete(EVM_WATCHED_ADDRESSES_CACHE_KEY)


def clear_evm_chain_tokens_cache(*, chain: Chain) -> None:
    """清空指定 EVM 链的 ERC20 合约缓存。"""

    cache.delete(_chain_tokens_cache_key(chain=chain))


def _chain_tokens_cache_key(*, chain: Chain) -> str:
    return EVM_CHAIN_TOKENS_CACHE_KEY_TEMPLATE.format(chain_id=chain.pk)


def _load_evm_watched_addresses_from_db() -> frozenset[str]:
    deposit_slot_addresses = (
        DepositSlot.objects.filter(
            chain__type=ChainType.EVM,
        )
        .values_list("address", flat=True)
        .iterator(chunk_size=EVM_WATCH_SET_ITERATOR_CHUNK_SIZE)
    )
    contract_pay_slot_addresses = (
        InvoicePaySlot.objects.filter(
            chain__type=ChainType.EVM,
            billing_mode=InvoiceBillingMode.CONTRACT,
            status=InvoicePaySlotStatus.ACTIVE,
            invoice__status=InvoiceStatus.WAITING,
        )
        .values_list("pay_address", flat=True)
        .iterator(chunk_size=EVM_WATCH_SET_ITERATOR_CHUNK_SIZE)
    )

    return frozenset(
        _normalize_address(address)
        for address in iter_chain(deposit_slot_addresses, contract_pay_slot_addresses)
    )


def _load_evm_chain_tokens_from_db(*, chain: Chain) -> dict[str, ChainToken]:
    token_rows = (
        ChainToken.objects.select_related("crypto")
        .filter(
            chain=chain,
            crypto__active=True,
        )
        .exclude(address="")
    )
    return {_normalize_address(token.address): token for token in token_rows}
