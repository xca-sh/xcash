from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_delete
from django.db.models.signals import post_save
from django.dispatch import receiver

from chains.constants import ChainType
from currencies.models import Crypto
from currencies.models import CryptoOnChain
from evm.scanner.watchers import clear_token_registry_cache
from evm.scanner.watchers import load_token_registry

# 保存 Crypto 时，只有这些字段之外的变更才可能改变 EVM 代币表内容。
# 代币表由 _load_token_registry_from_db 决定：它按 crypto__active 过滤，并借 select_related
# 把整个 Crypto 对象一并放进缓存供扫描器引用（token.crypto → ParsedEvmTransferLog.crypto），
# 所以除下列字段外一律按“可能相关”处理，新增字段默认走失效路径，不会因为漏改这里而读到脏对象。
#
# prices / prices_updated_at 是唯一被高频写入的字段：currencies.tasks.refresh_crypto_prices
# 每 60 秒对每个带 coingecko_id 的币执行 save(update_fields=["prices", "prices_updated_at"])，
# 而扫描链路从不读取这两个字段（法币计价发生在账单侧且实时回源 DB）。若把它们当作相关字段，
# N 个币 × M 条 EVM 链的代币表缓存每分钟都要清空重建一次，且 cache.delete 与 on_commit 重建
# 之间的空窗还会被每 2 秒一轮的链扫描各自回源，纯属无谓抖动。
CRYPTO_FIELDS_IRRELEVANT_TO_TOKEN_REGISTRY = frozenset({"prices", "prices_updated_at"})


def crypto_change_affects_token_registry(
    update_fields: frozenset[str] | None,
) -> bool:
    """判断本次 Crypto 保存是否可能改变 EVM 代币表内容。

    update_fields 为 None（全量 save）时拿不到字段信息，一律按“可能相关”处理：
    宁可多重建一次缓存，也不能让扫描器长期拿着过期的 Crypto。
    """
    if update_fields is None:
        return True
    return not set(update_fields).issubset(CRYPTO_FIELDS_IRRELEVANT_TO_TOKEN_REGISTRY)


def _refresh_token_registry_for_crypto_on_chain(
    *, crypto_on_chain: CryptoOnChain
) -> None:
    chain = crypto_on_chain.chain
    if chain.type != ChainType.EVM:
        return
    clear_token_registry_cache(chain=chain)
    transaction.on_commit(lambda: load_token_registry(chain=chain, refresh=True))


def _refresh_token_registries_for_crypto(*, crypto: Crypto) -> None:
    chains = [
        crypto_on_chain.chain
        for crypto_on_chain in (
            CryptoOnChain.objects.select_related("chain").filter(
                crypto=crypto,
                chain__type=ChainType.EVM,
            )
        )
    ]
    for chain in chains:
        clear_token_registry_cache(chain=chain)

    def refresh_chain_token_registries() -> None:
        for chain in chains:
            load_token_registry(chain=chain, refresh=True)

    transaction.on_commit(refresh_chain_token_registries)


@receiver(post_save, sender=CryptoOnChain)
@receiver(post_delete, sender=CryptoOnChain)
def refresh_token_registry_when_crypto_on_chain_changes(
    sender,
    instance: CryptoOnChain,
    **kwargs,
):
    _refresh_token_registry_for_crypto_on_chain(crypto_on_chain=instance)


@receiver(post_save, sender=Crypto)
def refresh_token_registry_when_crypto_changes(sender, instance: Crypto, **kwargs):
    if not crypto_change_affects_token_registry(kwargs.get("update_fields")):
        return
    _refresh_token_registries_for_crypto(crypto=instance)
