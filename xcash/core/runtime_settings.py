from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
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


def get_admin_sensitive_action_otp_max_age_seconds() -> int:
    system_settings = get_system_settings()
    if system_settings is not None:
        return int(system_settings.admin_sensitive_action_otp_max_age_seconds)
    return int(settings.ADMIN_SENSITIVE_ACTION_OTP_MAX_AGE_SECONDS)


def get_alerts_repeat_interval_minutes() -> int:
    system_settings = get_system_settings()
    if system_settings is not None:
        return int(system_settings.alerts_repeat_interval_minutes)
    return int(settings.ALERTS_REPEAT_INTERVAL_MINUTES)


def get_webhook_delivery_breaker_threshold() -> int:
    system_settings = get_system_settings()
    if system_settings is not None:
        return int(system_settings.webhook_delivery_breaker_threshold)
    return 30


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


def get_reviewing_withdrawal_timeout() -> timedelta:
    system_settings = get_system_settings()
    if system_settings is not None:
        return timedelta(minutes=system_settings.reviewing_withdrawal_timeout_minutes)
    return timedelta(minutes=30)


def get_pending_withdrawal_timeout() -> timedelta:
    system_settings = get_system_settings()
    if system_settings is not None:
        return timedelta(minutes=system_settings.pending_withdrawal_timeout_minutes)
    return timedelta(minutes=15)


def get_confirming_withdrawal_timeout() -> timedelta:
    system_settings = get_system_settings()
    if system_settings is not None:
        return timedelta(
            minutes=system_settings.confirming_withdrawal_timeout_minutes
        )
    return timedelta(minutes=30)


def get_webhook_event_timeout() -> timedelta:
    system_settings = get_system_settings()
    if system_settings is not None:
        return timedelta(minutes=system_settings.webhook_event_timeout_minutes)
    return timedelta(minutes=15)


def get_risk_marking_enabled() -> bool:
    system_settings = get_system_settings()
    if system_settings is not None:
        return bool(system_settings.risk_marking_enabled)
    return False


def get_risk_marking_threshold_usd() -> Decimal:
    system_settings = get_system_settings()
    if system_settings is not None:
        return Decimal(system_settings.risk_marking_threshold_usd)
    return Decimal("0")


def get_risk_marking_cache_seconds() -> int:
    system_settings = get_system_settings()
    if system_settings is not None:
        return int(system_settings.risk_marking_cache_seconds)
    return 3600


def get_risk_marking_force_refresh_threshold_usd() -> Decimal:
    system_settings = get_system_settings()
    if system_settings is not None:
        return Decimal(system_settings.risk_marking_force_refresh_threshold_usd)
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
