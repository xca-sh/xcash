from __future__ import annotations

from dataclasses import dataclass
from itertools import chain as iter_chain

from django.core.cache import cache
from web3 import Web3

from chains.models import Address
from chains.models import Chain
from chains.models import ChainType
from currencies.models import ChainToken
from projects.models import RecipientAddress
from projects.models import RecipientAddressUsage


@dataclass(frozen=True)
class EvmWatchSet:
    """描述某条 EVM 链当前需要关注的地址和代币集合。"""

    watched_addresses: frozenset[str]
    tokens_by_address: dict[str, ChainToken]


EVM_WATCHED_ADDRESSES_CACHE_KEY = "evm:scanner:watched_addresses"
EVM_SYSTEM_ADDRESSES_CACHE_KEY = "evm:scanner:system_addresses"
EVM_CHAIN_TOKENS_CACHE_KEY_TEMPLATE = "evm:scanner:chain_tokens:{chain_id}"
EVM_WATCH_SET_CACHE_TIMEOUT = None
EVM_WATCH_SET_ITERATOR_CHUNK_SIZE = 1_000


def _normalize_address(address: str) -> str:
    # 扫描器统一将地址标准化为 checksum，保证 DB 数据与 RPC 返回值可直接比对。
    return Web3.to_checksum_address(str(address))


def load_watch_set(*, chain: Chain, refresh: bool = False) -> EvmWatchSet:
    """加载某条链上需要监听的系统地址与受支持 ERC20 合约集合。"""

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


def load_evm_system_addresses(*, refresh: bool = False) -> frozenset[str]:
    """加载系统 EVM Address 集合，用于判断 tx.from 是否内部地址。"""
    if refresh:
        return refresh_evm_system_addresses()

    system_addresses = cache.get(EVM_SYSTEM_ADDRESSES_CACHE_KEY)
    if system_addresses is None:
        system_addresses = refresh_evm_system_addresses()
    return system_addresses


def refresh_evm_system_addresses() -> frozenset[str]:
    system_addresses = _load_evm_system_addresses_from_db()
    cache.set(
        EVM_SYSTEM_ADDRESSES_CACHE_KEY,
        system_addresses,
        timeout=EVM_WATCH_SET_CACHE_TIMEOUT,
    )
    return system_addresses


def refresh_evm_watched_addresses() -> frozenset[str]:
    """重建 EVM 全局观察地址缓存。

    Address/RecipientAddress 当前只区分 chain_type，不绑定具体 EVM chain；
    因此地址观察集是 EVM 全局缓存，避免每条链扫描时重复全表加载。
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
    cache.delete(EVM_SYSTEM_ADDRESSES_CACHE_KEY)


def clear_evm_system_addresses_cache() -> None:
    """清空 EVM 系统地址缓存。"""

    cache.delete(EVM_SYSTEM_ADDRESSES_CACHE_KEY)


def clear_evm_chain_tokens_cache(*, chain: Chain) -> None:
    """清空指定 EVM 链的 ERC20 合约缓存。"""

    cache.delete(_chain_tokens_cache_key(chain=chain))


def _chain_tokens_cache_key(*, chain: Chain) -> str:
    return EVM_CHAIN_TOKENS_CACHE_KEY_TEMPLATE.format(chain_id=chain.pk)


def _load_evm_watched_addresses_from_db() -> frozenset[str]:
    system_addresses = _load_evm_system_addresses_from_db()
    recipient_addresses = (
        RecipientAddress.objects.filter(
            chain_type=ChainType.EVM,
            usage__in=(
                RecipientAddressUsage.INVOICE,
                RecipientAddressUsage.DEPOSIT_COLLECTION,
            ),
        )
        .values_list("address", flat=True)
        .iterator(chunk_size=EVM_WATCH_SET_ITERATOR_CHUNK_SIZE)
    )

    return frozenset(
        _normalize_address(address)
        for address in iter_chain(system_addresses, recipient_addresses)
    )


def _load_evm_system_addresses_from_db() -> frozenset[str]:
    addresses = (
        Address.objects.filter(chain_type=ChainType.EVM)
        .values_list("address", flat=True)
        .iterator(chunk_size=EVM_WATCH_SET_ITERATOR_CHUNK_SIZE)
    )
    return frozenset(_normalize_address(address) for address in addresses)


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
