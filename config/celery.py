import os

from celery import Celery
from celery.schedules import crontab

from config.performance import get_int
from config.performance import get_int_default

# set the default Django settings module for the 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("xcash")

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Load task modules from all registered Django app configs.
app.autodiscover_tasks()


WEBHOOK_EVENTS_SCHEDULE_SECONDS = get_int_default(
    "CELERY_WEBHOOK_EVENTS_SCHEDULE_SECONDS",
    15,
)
LATEST_BLOCK_SCHEDULE_SECONDS = get_int_default(
    "CELERY_LATEST_BLOCK_SCHEDULE_SECONDS",
    16,
)
FALLBACK_PROCESS_TRANSFER_SCHEDULE_SECONDS = get_int_default(
    "CELERY_FALLBACK_PROCESS_TRANSFER_SCHEDULE_SECONDS",
    20,
)
EVM_BROADCAST_SCHEDULE_SECONDS = get_int_default(
    "CELERY_EVM_BROADCAST_SCHEDULE_SECONDS",
    8,
)
EVM_RESCAN_SCHEDULE_SECONDS = get_int_default(
    "CELERY_EVM_RESCAN_SCHEDULE_SECONDS",
    45,
)
TRON_SCAN_SCHEDULE_SECONDS = get_int(
    "CELERY_TRON_SCAN_SCHEDULE_SECONDS",
    "tron_scan_seconds",
)
INVOICE_EXPIRED_SCHEDULE_SECONDS = get_int_default(
    "CELERY_INVOICE_EXPIRED_SCHEDULE_SECONDS",
    60,
)
OPERATIONAL_RISKS_SCHEDULE_SECONDS = get_int_default(
    "CELERY_OPERATIONAL_RISKS_SCHEDULE_SECONDS",
    120,
)
CRYPTO_PRICE_REFRESH_SCHEDULE_SECONDS = get_int_default(
    "CELERY_CRYPTO_PRICE_REFRESH_SCHEDULE_SECONDS",
    120,
)


# ---------------------------
# webhooks app
# ---------------------------
webhooks_tasks = {
    "schedule_events": {
        "task": "webhooks.tasks.schedule_events",
        "schedule": WEBHOOK_EVENTS_SCHEDULE_SECONDS,
    },
}

# ---------------------------
# chains app
# ---------------------------
chains_tasks = {
    "update_latest_block": {
        "task": "chains.tasks.update_latest_block",
        "schedule": LATEST_BLOCK_SCHEDULE_SECONDS,
    },
    "fallback_process_transfer": {
        "task": "chains.tasks.fallback_process_transfer",
        "schedule": FALLBACK_PROCESS_TRANSFER_SCHEDULE_SECONDS,
    },
}

# ---------------------------
# evm app
# ---------------------------
evm_tasks = {
    "dispatch_due_evm_tx_tasks": {
        "task": "evm.tasks.dispatch_due_evm_tx_tasks",
        "schedule": EVM_BROADCAST_SCHEDULE_SECONDS,
    },
    "rescan_pending_evm_chains": {
        # 兜底：主扫描漏扫导致的 PENDING_CHAIN 卡单，周期性按 receipt 主动命中并定点重扫。
        "task": "evm.tasks.rescan_pending_evm_chains",
        "schedule": EVM_RESCAN_SCHEDULE_SECONDS,
    },
}

# ---------------------------
# currencies app
# ---------------------------
currencies_tasks = {
    "refresh_crypto_prices": {
        "task": "currencies.tasks.refresh_crypto_prices",
        "schedule": CRYPTO_PRICE_REFRESH_SCHEDULE_SECONDS,
    },
}

# ---------------------------
# tron app
# ---------------------------
tron_tasks = {
    "scan_active_tron_chains": {
        "task": "tron.tasks.scan_active_tron_chains",
        "schedule": TRON_SCAN_SCHEDULE_SECONDS,
    },
}

# ---------------------------
# invoices app
# ---------------------------
invoices_tasks = {
    "fallback_invoice_expired": {
        "task": "invoices.tasks.fallback_invoice_expired",
        "schedule": INVOICE_EXPIRED_SCHEDULE_SECONDS,
    },
}

# ---------------------------
# core app
# ---------------------------
core_tasks = {
    "scan_operational_risks": {
        # 巡检提币、归集、Webhook 卡单风险；告警先走结构化日志，后续再接外部通知渠道。
        "task": "core.tasks.scan_operational_risks",
        "schedule": OPERATIONAL_RISKS_SCHEDULE_SECONDS,
    },
}

# ---------------------------
# celery 内置
# ---------------------------
celery_internal_tasks = {
    "backend_cleanup": {
        "task": "celery.backend_cleanup",
        "schedule": crontab(hour=4, minute=0, day_of_week=1),
    },
}

# ---------------------------
# 最终合并
# ---------------------------
app.conf.beat_schedule = {
    **webhooks_tasks,
    **chains_tasks,
    **evm_tasks,
    **tron_tasks,
    **currencies_tasks,
    **celery_internal_tasks,
    **invoices_tasks,
    **core_tasks,
}
