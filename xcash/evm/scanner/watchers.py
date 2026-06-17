from __future__ import annotations

import structlog
from django.core.cache import cache

from chains.models import Chain
from chains.models import VaultSlot
from chains.models import VaultSlotUsage
from currencies.models import CryptoOnChain

logger = structlog.get_logger()

TOKEN_REGISTRY_CACHE_KEY_TEMPLATE = "evm:scanner:token_registry:{chain_id}"


def load_token_registry(
    *, chain: Chain, refresh: bool = False
) -> dict[str, CryptoOnChain]:
    """加载某条 EVM 链当前受支持的 ERC20 代币表，按合约地址索引。

    代币表是 per-chain 的静态配置（CryptoOnChain 后台手动维护），扫描前一次性加载
    并长驻缓存。它只回答“关注哪些代币”，与“本轮命中了哪些系统自有收款地址”是两件事，
    后者按日志窗口走 load_owned_addresses_for_candidates，两者职责互不相干。

    缓存 miss 或 refresh=True 时回源 DB 并写穿缓存；CryptoOnChain 在后台变更后由
    evm.signals 以 refresh=True 主动重建，所以这里没有独立的对外刷新入口。
    """

    cache_key = _token_registry_cache_key(chain=chain)
    tokens_by_address = cache.get(cache_key)
    if refresh or tokens_by_address is None:
        tokens_by_address = _load_token_registry_from_db(chain=chain)
        # timeout=None 表示永不过期，依赖显式 refresh（CryptoOnChain 表为后台手动配置，几乎不变）。
        cache.set(cache_key, tokens_by_address, timeout=None)
    return tokens_by_address


def clear_token_registry_cache(*, chain: Chain | None = None) -> None:
    """清空 EVM 代币表缓存。

    指定 chain 时只清该链（CryptoOnChain 变更后的失效，见 evm.signals）；不指定时
    清所有链，主要用于测试与运维脚本。
    """

    if chain is not None:
        cache.delete(_token_registry_cache_key(chain=chain))
        return
    delete_pattern = getattr(cache, "delete_pattern", None)
    if callable(delete_pattern):
        delete_pattern(TOKEN_REGISTRY_CACHE_KEY_TEMPLATE.format(chain_id="*"))


def load_owned_addresses_for_candidates(
    *,
    chain: Chain,
    addresses: set[str] | frozenset[str],
) -> frozenset[str]:
    """从本轮日志候选地址中批量筛出系统自有的收款地址。

    自有收款地址来自 VaultSlot 与 DifferRecipientAddress；不在扫描前全量加载，
    而是在每个日志窗口内按候选地址即时匹配，所以与代币表分开维护。
    """

    if not addresses:
        return frozenset()

    candidates = set(addresses)
    vault_slot_addresses = set(
        VaultSlot.objects.filter(
            chain=chain,
            address__in=candidates,
        ).values_list("address", flat=True)
    )
    # 充值地址按 (project, customer) 在同一链类型内确定唯一、与具体网络无关（salt 不掺
    # chain，factory/implementation 全网同址）。客户可能只在别的 EVM 链取过该地址，本链
    # 尚无 VaultSlot 行，导致同地址的跨链充值被静默丢弃。这里对这类候选按需补建本链槽位，
    # 把识别从「本链行已存在」解耦为「能复算认出客户」。
    vault_slot_addresses |= ensure_cross_chain_deposit_slots(
        chain=chain,
        candidates=candidates - vault_slot_addresses,
    )
    from invoices.models import DifferRecipientAddress

    differ_addresses = DifferRecipientAddress.matched_addresses_for_candidates(
        chain=chain,
        candidates=candidates,
    )
    return frozenset(vault_slot_addresses | differ_addresses)


def ensure_cross_chain_deposit_slots(
    *,
    chain: Chain,
    candidates: set[str],
) -> set[str]:
    """为「已是其它 EVM 链充值槽位、本链却无对应行」的候选地址按需补建本链 VaultSlot。

    只处理 DEPOSIT 用途：账单（INVOICE）收款是按指定链发生的，跨链付款属于「付错链」，
    语义不同，不在此自动补建。返回本轮成功补建（或已存在）的、确属本链可归集的地址集合。
    """
    if not candidates:
        return set()

    source_slots = (
        VaultSlot.objects.filter(
            usage=VaultSlotUsage.DEPOSIT,
            chain__type=chain.type,
            address__in=candidates,
        )
        .exclude(chain=chain)
        .select_related("project", "customer", "chain")
    )

    materialized: set[str] = set()
    for source in source_slots:
        if materialize_deposit_slot_on_chain(chain=chain, source=source):
            materialized.add(source.address)
    return materialized


def materialize_deposit_slot_on_chain(*, chain: Chain, source: VaultSlot) -> bool:
    """把另一条 EVM 链上的充值槽位在本链落地为 VaultSlot 行，补建前做复算校验。

    复算校验是这条跨链识别路径的安全闸门：用本链 factory/implementation/vault/salt
    重新预测地址，必须与候选完全一致才补建。地址不符说明本链合约配置与全网不一致、该
    地址在本链不可部署归集，拒绝补建以免记入一笔无法归集的充值。复算是纯本地计算，不打 RPC。
    """
    from evm.vault_slots import predict_address

    vault_address = source.project.vault_address_for_chain_type(chain.type)
    if not vault_address:
        return False

    salt = bytes(source.salt)
    predicted = predict_address(chain=chain, vault=vault_address, salt=salt)
    if predicted != source.address:
        logger.warning(
            "跨链充值地址在本链复算不一致，拒绝补建 VaultSlot",
            chain=chain.code,
            source_chain=source.chain.code,
            address=source.address,
            predicted=predicted,
        )
        return False

    # get_or_create 受 (customer, chain) / (chain, address) 唯一约束保护，重放窗口或
    # 多 worker 并发补建同一槽位时由唯一约束收口，IntegrityError 被 get_or_create 吞掉后
    # 回退为 get，幂等安全。不在此触发部署：ERC20 充值的部署延迟到归集前置闸门按需进行。
    VaultSlot.objects.get_or_create(
        chain=chain,
        project=source.project,
        usage=VaultSlotUsage.DEPOSIT,
        customer=source.customer,
        defaults={"address": source.address, "salt": salt},
    )
    return True


def _token_registry_cache_key(*, chain: Chain) -> str:
    """构造按链区分的代币表缓存 key。"""
    return TOKEN_REGISTRY_CACHE_KEY_TEMPLATE.format(chain_id=chain.pk)


def _load_token_registry_from_db(*, chain: Chain) -> dict[str, CryptoOnChain]:
    """从 DB 拉取本链已激活 ERC20，按合约地址建立索引（绕过缓存的回源查询）。"""
    token_rows = (
        CryptoOnChain.objects.select_related("crypto")
        .filter(
            chain=chain,
            crypto__active=True,
            active=True,
        )
        .exclude(address="")
    )
    for token in token_rows:
        token.normalize_address_for_chain()
    return {token.address: token for token in token_rows}
