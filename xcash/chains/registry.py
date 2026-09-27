"""chains 引擎的扩展点注册表。

chains 是链引擎内核：扫描、确认、VaultSlot、链上任务都在这里编排。它不应该知道
具体有哪些链族实现（evm / tron），也不应该知道哪些业务在消费入账（invoices /
deposits）。这些上层模块在各自 AppConfig.ready() 里把实现登记到这里，chains 只
面向本模块定义的契约调用，依赖方向因此始终是「上层 → chains」（由 import-linter
的分层契约锁定，见 pyproject.toml 的 [tool.importlinter]）。

三类扩展点：
- 链族 ChainFamily：链适配器与 VaultSlot 后端。新增链族 = 新建 app 并登记，不改 chains。
- 入账业务处理器 TransferHandler：把一笔外部入账匹配为某类业务，确认后推进该业务。
- 收款地址来源 RecipientAddressSource：VaultSlot 之外、扫描器也必须识别的业务收款地址。

登记只发生在进程启动的 ready() 阶段，之后只读，因此不需要加锁。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol

from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from chains.adapters import AdapterInterface
    from chains.constants import VaultSlotContractAddresses
    from chains.models import Chain
    from chains.models import Transfer
    from chains.models import TxTask
    from chains.models import VaultSlot


class VaultSlotBackend(Protocol):
    """链族 VaultSlot 后端契约，evm.vault_slots / tron.vault_slots 模块按此实现。"""

    def contract_addresses(self, chain: Chain) -> VaultSlotContractAddresses: ...

    def predict_address(self, *, chain: Chain, vault: str, salt: bytes) -> str: ...

    def is_deployed_on_chain(self, *, chain: Chain, address: str) -> bool: ...

    def create_deploy_tx_task(self, *, slot: VaultSlot) -> TxTask: ...

    def estimate_collect_gas(
        self, *, chain: Chain, crypto: Any, slot: VaultSlot
    ) -> int | None: ...

    def create_collect_tx_task(
        self,
        *,
        chain: Chain,
        crypto: Any,
        slot: VaultSlot,
        collect_gas_hint: int | None = None,
    ) -> TxTask: ...


@dataclass(frozen=True, kw_only=True)
class ChainFamily:
    """一个链族（同一套地址格式、交易模型与合约部署方式的链集合）的实现。"""

    chain_type: str
    # 登记类而非实例：沿用原 AdapterFactory 每次调用新建实例的语义，避免跨调用共享状态。
    adapter_class: type[AdapterInterface]
    vault_slot_backend: VaultSlotBackend


@dataclass(frozen=True, kw_only=True)
class TransferHandler:
    """一类业务对链上入账的处理契约。

    - try_match：尝试把外部入账匹配为本业务，匹配成功返回 True（并自行完成归类落库）。
    - try_match_initial_native_balance：VaultSlot 部署时合约内已有原生币余额，这笔
      「初始余额入账」由槽位用途对应的业务认领，签名为 (transfer, *, slot, payment_datetime)。
    - confirm：入账达到确认深度后推进业务（账单完成、充值记账等）。

    三个回调都必须在调用时再解析业务服务（写成模块级函数而非持有方法引用），
    否则测试对服务方法的 patch 不会生效。
    """

    transfer_type: str
    vault_slot_usage: str
    # 外部入账按此升序逐一尝试匹配，先匹配者胜；同一地址理论上只属于一类业务，
    # 顺序只在地址冲突的异常场景下决定归属，因此必须显式声明而不能依赖 app 加载顺序。
    match_order: int
    try_match: Callable[[Transfer], bool]
    try_match_initial_native_balance: Callable[..., bool]
    confirm: Callable[[Transfer], Any]


@dataclass(frozen=True, kw_only=True)
class RecipientAddressSource:
    """VaultSlot 之外的业务收款地址来源，签名为 (*, chain, candidates) -> set[str]。"""

    name: str
    match_addresses: Callable[..., set[str]]


_chain_families: dict[str, ChainFamily] = {}
_transfer_handlers: dict[str, TransferHandler] = {}
_recipient_address_sources: dict[str, RecipientAddressSource] = {}


def register_entry(registry: dict, key: str, entry: Any, *, kind: str) -> None:
    """登记一个扩展实现；同一 key 重复登记必须是同一实现（ready 可能被重复调用）。"""
    existing = registry.get(key)
    if existing is not None and existing != entry:
        raise ImproperlyConfigured(f"{kind} {key!r} 被重复登记且实现不一致")
    registry[key] = entry


def register_chain_family(family: ChainFamily) -> None:
    register_entry(_chain_families, str(family.chain_type), family, kind="链族")


def get_chain_family(chain_type: str) -> ChainFamily:
    try:
        return _chain_families[str(chain_type)]
    except KeyError:
        raise ValueError(f"未登记的链类型: {chain_type}") from None


def register_transfer_handler(handler: TransferHandler) -> None:
    register_entry(
        _transfer_handlers, str(handler.transfer_type), handler, kind="入账业务处理器"
    )


def registered_transfer_types() -> set[str]:
    return set(_transfer_handlers)


def transfer_handlers_in_match_order() -> list[TransferHandler]:
    return sorted(_transfer_handlers.values(), key=lambda handler: handler.match_order)


def require_transfer_handler(transfer_type: str) -> TransferHandler:
    """按业务归类取处理器；已归类却无处理器说明启动装配缺失，必须抛错而非静默跳过。"""
    try:
        return _transfer_handlers[str(transfer_type)]
    except KeyError:
        raise ImproperlyConfigured(
            f"入账归类 {transfer_type!r} 没有登记业务处理器"
        ) from None


def require_transfer_handler_for_vault_slot_usage(usage: str) -> TransferHandler:
    for handler in _transfer_handlers.values():
        if str(handler.vault_slot_usage) == str(usage):
            return handler
    raise ImproperlyConfigured(f"VaultSlot 用途 {usage!r} 没有登记业务处理器")


def register_recipient_address_source(source: RecipientAddressSource) -> None:
    register_entry(_recipient_address_sources, source.name, source, kind="收款地址来源")


def match_recipient_addresses(*, chain: Chain, candidates: set[str]) -> set[str]:
    """返回候选地址中属于任一已登记业务收款地址来源的部分。"""
    if not candidates:
        return set()
    matched: set[str] = set()
    for source in _recipient_address_sources.values():
        matched |= source.match_addresses(chain=chain, candidates=candidates)
    return matched
