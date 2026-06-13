from __future__ import annotations

from dataclasses import dataclass

from django.db import models


class ChainCode(models.TextChoices):
    Ethereum = "ethereum", "Ethereum"
    BSC = "bsc", "BSC"
    Polygon = "polygon", "Polygon"
    ArbitrumOne = "arbitrum-one", "Arbitrum One"
    Optimism = "optimism", "Optimism"
    Base = "base", "Base"
    Avalanche = "avalanche", "Avalanche C-Chain"
    Linea = "linea", "Linea"
    Scroll = "scroll", "Scroll"
    Tron = "tron", "Tron"
    Sepolia = "sepolia", "Sepolia"
    Nile = "nile", "Nile"
    Anvil = "anvil", "Anvil Local"


class ChainType(models.TextChoices):
    EVM = "evm", "EVM"
    TRON = "tron", "Tron"


@dataclass(frozen=True)
class ChainSpec:
    type: str
    chain_id: int | None
    is_poa: bool | None
    confirm_block_count: int
    native_coin_symbol: str
    native_coin_decimals: int
    # 该链两次扫描之间的最小间隔（秒）。调度器固定每 2 秒巡检一次，
    # 仅放行 now - last_scanned_at ≥ scan_interval_seconds 的链，
    # 以此为不同出块速度的链设置各自的扫描节奏。取值通常贴近出块时间，
    # 低于调度粒度（2 秒）也只会按每轮巡检触发，不会更快。
    scan_interval_seconds: int
    # 是否为测试网（非主网）。链固有属性，仅用于区分/展示，不参与扫描与签名逻辑。
    # 作为带默认值的尾部字段，主网无需逐条改写既有定义。
    is_testnet: bool = False
    # 链图标 URL，供 SaaS 前端按链展示。单一权威来源在此，新增链只改这里；
    # 当前用 DefiLlama 公开链图标 CDN（见 _icon），后续可逐条换成自托管 URL。
    # 测试网/本地链无需图标，留空即可。
    icon: str = ""


@dataclass(frozen=True)
class VaultSlotContractAddresses:
    factory: str = ""
    implementation: str = ""


def _icon(slug: str) -> str:
    """DefiLlama 公开链图标 CDN。slug 为其链命名（与本系统 code 不机械对应，
    如 bsc→binance、arbitrum-one→arbitrum），故逐条显式指定而非由 code 派生。"""
    return f"https://icons.llamao.fi/icons/chains/rsz_{slug}.jpg"


CHAIN_SPECS: dict[str, ChainSpec] = {
    ChainCode.Ethereum: ChainSpec(
        ChainType.EVM, 1, False, 12, "ETH", 18, 12, icon=_icon("ethereum")
    ),
    ChainCode.BSC: ChainSpec(
        ChainType.EVM, 56, True, 15, "BNB", 18, 6, icon=_icon("binance")
    ),
    ChainCode.Polygon: ChainSpec(
        ChainType.EVM, 137, True, 128, "POL", 18, 6, icon=_icon("polygon")
    ),
    ChainCode.ArbitrumOne: ChainSpec(
        ChainType.EVM, 42161, False, 20, "ETH", 18, 4, icon=_icon("arbitrum")
    ),
    ChainCode.Optimism: ChainSpec(
        ChainType.EVM, 10, False, 20, "ETH", 18, 4, icon=_icon("optimism")
    ),
    ChainCode.Base: ChainSpec(
        ChainType.EVM, 8453, False, 20, "ETH", 18, 4, icon=_icon("base")
    ),
    ChainCode.Avalanche: ChainSpec(
        ChainType.EVM, 43114, False, 8, "AVAX", 18, 6, icon=_icon("avalanche")
    ),
    ChainCode.Linea: ChainSpec(
        ChainType.EVM, 59144, False, 20, "ETH", 18, 6, icon=_icon("linea")
    ),
    ChainCode.Scroll: ChainSpec(
        ChainType.EVM, 534352, False, 20, "ETH", 18, 6, icon=_icon("scroll")
    ),
    # Anvil 是本地联调链，资产没有生产价值，必须归入测试资产域。
    ChainCode.Anvil: ChainSpec(
        ChainType.EVM, 31337, False, 8, "ETH", 18, 4, is_testnet=True
    ),
    ChainCode.Tron: ChainSpec(
        ChainType.TRON, None, None, 16, "TRX", 6, 6, icon=_icon("tron")
    ),
    ChainCode.Sepolia: ChainSpec(
        ChainType.EVM, 11155111, False, 12, "ETH", 18, 12, is_testnet=True
    ),
    ChainCode.Nile: ChainSpec(
        ChainType.TRON, None, None, 16, "TRX", 6, 6, is_testnet=True
    ),
}


# 系统已知的链原生币符号集合，作为 Crypto.is_native 的合法域来源。
# 直接从 CHAIN_SPECS 派生，避免在别处再硬编码一份易漂移的名单。
NATIVE_COIN_SYMBOLS: frozenset[str] = frozenset(
    spec.native_coin_symbol for spec in CHAIN_SPECS.values()
)


# 原生币的 CoinGecko 行情 slug。原生币会被自动建成 active=True 的 Crypto，
# 必须立刻具备可刷新的真实币价（否则 price()/to_fiat()/scale 等会 KeyError，
# 直接卡死 invoice 金额换算等核心链路）。slug 与 symbol 无机械对应（BNB→binancecoin、
# AVAX→avalanche-2），故在此显式建权威映射；以 symbol 为键，因多条链共享同一原生币
# （各 L2 都用 ETH）会 get_or_create 到同一 Crypto，slug 天然一致。
NATIVE_COIN_COINGECKO_IDS: dict[str, str] = {
    "ETH": "ethereum",
    "BNB": "binancecoin",
    "POL": "polygon",
    "AVAX": "avalanche-2",
    "TRX": "tron",
}

# 覆盖性断言：任何新接入链的原生币都必须在此登记 slug，否则它建出来就没币价。
# 在导入期失败，把"漏配"暴露在部署前而非运行时。
_missing_native_slugs = NATIVE_COIN_SYMBOLS - NATIVE_COIN_COINGECKO_IDS.keys()
if _missing_native_slugs:
    raise RuntimeError(
        f"原生币缺少 CoinGecko slug 映射：{sorted(_missing_native_slugs)}，"
        "请在 NATIVE_COIN_COINGECKO_IDS 中补齐。"
    )


EVM_CHAIN_CODES: tuple[str, ...] = tuple(
    code for code, spec in CHAIN_SPECS.items() if spec.type == ChainType.EVM
)
TRON_CHAIN_CODES: tuple[str, ...] = tuple(
    code for code, spec in CHAIN_SPECS.items() if spec.type == ChainType.TRON
)


# Tron / TVM 的基础合约部署地址按网络独立维护：主网与 Nile 不共享地址。
# 当前先留空，完成各网络部署与验收后只需补充这张表。
TRON_VAULT_SLOT_CONTRACT_ADDRESSES: dict[str, VaultSlotContractAddresses] = {
    ChainCode.Tron: VaultSlotContractAddresses(),
    ChainCode.Nile: VaultSlotContractAddresses(
        factory="TT6P5YyGYwfDyc4SE6HhQLyCsXcv7M9FyV",
        implementation="THYuW8NBNSCAEN7UFqhz385vsYGssncsHz",
    ),
}

# Tron HTTP 网关地址固定两套：主网与测试网（Nile）。按 Chain.is_testnet 二选一，
# 不落库成字段，避免运维填错或与 code 漂移。
TRON_MAINNET_BASE_URL = "https://api.trongrid.io"
TRON_TESTNET_BASE_URL = "https://nile.trongrid.io"
