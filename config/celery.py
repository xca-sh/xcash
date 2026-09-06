import os

from celery import Celery
from celery.schedules import crontab
from celery.signals import before_task_publish
from celery.signals import celeryd_after_setup

from config.performance import get_int_default
from config.periodic_tasks import PERIODIC_TASK_GROUPS
from config.periodic_tasks import PERIODIC_TASK_QUEUES
from config.periodic_tasks import WORKER_HEALTH_TASK_GROUPS
from config.worker_health import WORKER_HEALTH_INTERVAL_SECONDS
from config.worker_health import stamp_worker_health_probe

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
before_task_publish.connect(stamp_worker_health_probe)


@celeryd_after_setup.connect
def subscribe_periodic_queues(sender, instance, **kwargs):  # noqa: ARG001
    """-Q 仍选择业务分组；在 consumer 启动前添加该组的周期专属队列。"""
    queues = instance.app.amqp.queues
    selected = set(queues.consume_from)
    for task, group in PERIODIC_TASK_GROUPS.items():
        if group in selected:
            queues.select_add(PERIODIC_TASK_QUEUES[task])


WEBHOOK_EVENTS_SCHEDULE_SECONDS = get_int_default(
    "CELERY_WEBHOOK_EVENTS_SCHEDULE_SECONDS",
    15,
)
WEBHOOK_STALLED_REAP_SCHEDULE_SECONDS = get_int_default(
    "CELERY_WEBHOOK_STALLED_REAP_SCHEDULE_SECONDS",
    300,
)
FALLBACK_PROCESS_TRANSFER_SCHEDULE_SECONDS = get_int_default(
    "CELERY_FALLBACK_PROCESS_TRANSFER_SCHEDULE_SECONDS",
    20,
)
EVM_BROADCAST_SCHEDULE_SECONDS = get_int_default(
    "CELERY_EVM_BROADCAST_SCHEDULE_SECONDS",
    2,
)
# 扫描调度器固定每 2 秒巡检一次活跃链；具体每条链多久扫一次由
# ChainSpec.scan_interval_seconds 与 Chain.last_scanned_at 在任务内决定。
SCAN_DISPATCH_SCHEDULE_SECONDS = get_int_default(
    "CELERY_SCAN_DISPATCH_SCHEDULE_SECONDS",
    2,
)
EVM_NON_TRANSFER_CONFIRM_SCHEDULE_SECONDS = get_int_default(
    "CELERY_EVM_NON_TRANSFER_CONFIRM_SCHEDULE_SECONDS",
    60,
)
# EVM 在途主动交易（部署/归集）的终局轮询周期。该轮询原先内联在扫描任务里，
# 扫描一出异常就整段跳过；拆出来独立调度后周期取 15s——EvmTaskPoller 自身按
# EVM_PENDING_RECEIPT_POLL_DELAY(32s) 过滤刚广播的任务，再快也没有额外收益。
EVM_TX_TASK_POLL_SCHEDULE_SECONDS = get_int_default(
    "CELERY_EVM_TX_TASK_POLL_SCHEDULE_SECONDS",
    15,
)
VAULT_SLOT_COLLECT_SCHEDULE_SECONDS = get_int_default(
    "CELERY_VAULT_SLOT_COLLECT_SCHEDULE_SECONDS",
    60,
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
    60,
)


# ---------------------------
# webhooks app
# ---------------------------
webhooks_tasks = {
    "schedule_events": {
        "task": "webhooks.tasks.schedule_events",
        "schedule": WEBHOOK_EVENTS_SCHEDULE_SECONDS,
    },
    "reap_stalled_webhook_events": {
        # 强制终结超龄未送达事件，防止其恒占调度批次头部饿死其他商户的重试。
        "task": "webhooks.tasks.reap_stalled_events",
        "schedule": WEBHOOK_STALLED_REAP_SCHEDULE_SECONDS,
    },
}

# ---------------------------
# chains app
# ---------------------------
chains_tasks = {
    "fallback_process_transfer": {
        "task": "chains.tasks.fallback_process_transfer",
        "schedule": FALLBACK_PROCESS_TRANSFER_SCHEDULE_SECONDS,
    },
    "execute_due_vault_slot_collect_schedules": {
        "task": "chains.tasks.execute_due_vault_slot_collect_schedules",
        "schedule": VAULT_SLOT_COLLECT_SCHEDULE_SECONDS,
    },
    "reconcile_vault_slot_collect_balance_gaps": {
        "task": "chains.tasks.reconcile_vault_slot_collect_balance_gaps_task",
        "schedule": OPERATIONAL_RISKS_SCHEDULE_SECONDS,
    },
    "reap_stale_confirming_transfers": {
        # 每日清理链上已无事实的超龄 CONFIRMING 转账，防止其恒占确认批次头部。
        # 删除前逐条做链上终验（MISSING 才删），避免确认管线故障时误删真实入账。
        "task": "chains.tasks.reap_stale_confirming_transfers",
        "schedule": crontab(hour=4, minute=30),
    },
}

# ---------------------------
# evm app
# ---------------------------
evm_tasks = {
    "dispatch_evm_tx_tasks": {
        "task": "evm.tasks.dispatch_evm_tx_tasks",
        "schedule": EVM_BROADCAST_SCHEDULE_SECONDS,
    },
    "scan_active_evm_chains": {
        "task": "evm.tasks.scan_active_evm_chains",
        "schedule": SCAN_DISPATCH_SCHEDULE_SECONDS,
    },
    "scan_stuck_queued_evm_tx_tasks": {
        "task": "evm.tasks.scan_stuck_queued_evm_tx_tasks",
        "schedule": OPERATIONAL_RISKS_SCHEDULE_SECONDS,
    },
    "poll_active_evm_chains": {
        "task": "evm.tasks.poll_active_evm_chains",
        "schedule": EVM_TX_TASK_POLL_SCHEDULE_SECONDS,
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
    "dispatch_tron_tx_tasks": {
        "task": "tron.tasks.dispatch_tron_tx_tasks",
        "schedule": EVM_BROADCAST_SCHEDULE_SECONDS,
    },
    "scan_active_tron_chains": {
        "task": "tron.tasks.scan_active_tron_chains",
        "schedule": SCAN_DISPATCH_SCHEDULE_SECONDS,
    },
    "confirm_tron_receipt_tx_tasks": {
        "task": "tron.tasks.confirm_tron_receipt_tx_tasks",
        "schedule": EVM_NON_TRANSFER_CONFIRM_SCHEDULE_SECONDS,
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
    **{
        task: {"task": task, "schedule": WORKER_HEALTH_INTERVAL_SECONDS}
        for task in WORKER_HEALTH_TASK_GROUPS
    },
    "scan_operational_risks": {
        # 巡检 Webhook 卡单风险；告警先走结构化日志，后续再接外部通知渠道。
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
