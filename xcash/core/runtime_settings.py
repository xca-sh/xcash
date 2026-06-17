from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache

from core.models import SYSTEM_SETTINGS_CACHE_KEY
from core.models import SystemSettings

_MISSING_SYSTEM_SETTINGS = "__missing_system_settings__"


def get_system_settings() -> SystemSettings | None:
    # 系统参数读取频率高于写入频率，缓存单例记录可以避免每次请求都命中数据库。
    cached_value = cache.get(
        SYSTEM_SETTINGS_CACHE_KEY, default=_MISSING_SYSTEM_SETTINGS
    )
    if cached_value == _MISSING_SYSTEM_SETTINGS:
        system_settings = SystemSettings.objects.order_by("pk").first()
        cache.set(
            SYSTEM_SETTINGS_CACHE_KEY,
            system_settings or _MISSING_SYSTEM_SETTINGS,
            timeout=None,
        )
        return system_settings
    if cached_value is None:
        return None
    if cached_value != _MISSING_SYSTEM_SETTINGS:
        return cached_value
    return None


def get_admin_session_timeout_seconds() -> int:
    system_settings = get_system_settings()
    if system_settings is not None:
        return int(system_settings.admin_session_timeout_minutes) * 60
    return 10 * 60


def get_webhook_delivery_max_retries() -> int:
    system_settings = get_system_settings()
    if system_settings is not None:
        return int(system_settings.webhook_delivery_max_retries)
    return 5


def get_webhook_delivery_max_backoff_seconds() -> int:
    system_settings = get_system_settings()
    if system_settings is not None:
        return int(system_settings.webhook_delivery_max_backoff_seconds)
    return 120


def get_webhook_event_timeout() -> timedelta:
    system_settings = get_system_settings()
    if system_settings is not None:
        return timedelta(minutes=system_settings.webhook_event_timeout_minutes)
    return timedelta(minutes=15)


def get_vault_slot_collect_delay(chain_type: str) -> timedelta:
    # 归集延迟按链类型取：EVM 短（gas 便宜、优先到账），Tron 长（成本高、批量摊薄）。
    # 无 SystemSettings 记录时回退到与字段 default 一致的兜底值，避免漂移。
    from chains.models import ChainType

    field_by_type = {
        ChainType.EVM: "evm_vault_slot_collect_delay_minutes",
        ChainType.TRON: "tron_vault_slot_collect_delay_minutes",
    }
    fallback_minutes_by_type = {
        ChainType.EVM: 2,
        ChainType.TRON: 360,
    }
    if chain_type not in field_by_type:
        raise ValueError(f"VaultSlot 归集不支持链类型: {chain_type}")

    system_settings = get_system_settings()
    if system_settings is not None:
        return timedelta(minutes=getattr(system_settings, field_by_type[chain_type]))
    return timedelta(minutes=fallback_minutes_by_type[chain_type])


def get_invoice_vault_slot_limit_per_project_chain() -> int:
    system_settings = get_system_settings()
    if system_settings is not None:
        return int(system_settings.invoice_vault_slot_limit_per_project_chain)
    return 100


def get_aml_screening_enabled() -> bool:
    system_settings = get_system_settings()
    if system_settings is not None:
        return bool(system_settings.aml_screening_enabled)
    return False


def get_aml_screening_threshold_usd() -> Decimal:
    system_settings = get_system_settings()
    if system_settings is not None:
        return Decimal(system_settings.aml_screening_threshold_usd)
    return Decimal("0")


def get_aml_screening_cache_seconds() -> int:
    system_settings = get_system_settings()
    if system_settings is not None:
        return int(system_settings.aml_screening_cache_seconds)
    return 3600


def get_aml_screening_force_refresh_threshold_usd() -> Decimal:
    system_settings = get_system_settings()
    if system_settings is not None:
        return Decimal(system_settings.aml_screening_force_refresh_threshold_usd)
    return Decimal("10000")


def get_quicknode_misttrack_endpoint_url() -> str:
    system_settings = get_system_settings()
    if system_settings is not None:
        return system_settings.quicknode_misttrack_endpoint_url.strip()
    return ""


def get_misttrack_openapi_api_key() -> str:
    system_settings = get_system_settings()
    if system_settings is not None:
        return system_settings.misttrack_openapi_api_key.strip()
    return ""
