import gc
import os

from celery import Celery
from celery.beat import PersistentScheduler
from celery.schedules import crontab
from celery.signals import before_task_publish
from celery.signals import celeryd_after_setup
from celery.signals import worker_init

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
# evm / tron 的 SaaS gas 计费重试任务定义在各自的 saas_gas_billing 模块，不在 tasks 模块里。
# 默认 autodiscover 只加载 tasks，worker 主进程若没有经别处导入这两个模块，这些任务就不会注册：
# chain-runner 投递的重试会被 worker 当作 unregistered task 丢弃，gas 计费永久漏报。
# 因此按模块名显式发现；没有 saas_gas_billing 模块的 app 会被 Celery 静默跳过。
app.autodiscover_tasks(related_name="saas_gas_billing")
before_task_publish.connect(stamp_worker_health_probe)


@worker_init.connect
def freeze_startup_objects(sender, **kwargs):  # noqa: ARG001
    """把启动期常驻对象移出 GC 追踪，让 prefork 子进程持续与主进程共享这部分内存。

    子进程 fork 时与主进程写时复制共享启动期加载的 ~110MB（Django、web3、全部任务模块）。
    但 CPython 每次 GC 都会改写被追踪对象的头部，子进程一次全量回收就会把这些页全部
    复制成私有；生产 smaps_rollup 实测子进程与主进程只剩 12~27MB 共享。gc.freeze()
    把当前对象移入永久代、此后 GC 不再遍历它们，从而保住共享。

    worker_init 在主进程中发送：此时 loader.init_worker 已完成 django.setup 与任务模块
    导入，进程池尚未 fork，是唯一合适的时机。只冻结这一次；MAX_TASKS_PER_CHILD 回收后
    新 fork 的子进程同样继承冻结状态。不在每次 fork 前重复冻结，以免把主进程运行期间
    的循环垃圾也永久冻结、造成主进程缓慢增长。
    """
    gc.freeze()


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
VAULT_SLOT_COLLECT_SCHEDULE_SECONDS = get_int_default(
    "CELERY_VAULT_SLOT_COLLECT_SCHEDULE_SECONDS",
    60,
)
# 账单过期只靠这一个周期任务（不再逐单投递 ETA），周期即过期/槽位释放的最大延迟；
# 查询走 invoice_waiting_unpaid_idx 部分索引，只扫待支付未绑定转账的账单。
INVOICE_EXPIRED_SCHEDULE_SECONDS = get_int_default(
    "CELERY_INVOICE_EXPIRED_SCHEDULE_SECONDS",
    15,
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
# EVM / Tron 主动交易的派发、广播与终局轮询由 chain-runner 的按链循环负责，不经 beat。
evm_tasks = {
    "scan_stuck_queued_evm_tx_tasks": {
        "task": "evm.tasks.scan_stuck_queued_evm_tx_tasks",
        "schedule": OPERATIONAL_RISKS_SCHEDULE_SECONDS,
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
    **currencies_tasks,
    **celery_internal_tasks,
    **invoices_tasks,
    **core_tasks,
}


class WorkerHealthFirstScheduler(PersistentScheduler):
    """Beat 一启动就投递 worker 健康探针，其余周期任务保持 Celery 原有语义。

    Celery 以启动时刻作为调度文件里没有记录的条目的上次执行时间，首次投递要等满一个
    周期；beat 容器每次随新镜像重建都会丢失调度文件，升级脚本等调度回执因此固定多等
    一个探针周期。把探针的上次执行时间回拨一个周期，让它在首个 tick 即到期：新 Beat
    的调度循环一跑起来就产生回执，且该回执仍出自真实调度，不是启动钩子代发。
    """

    def setup_schedule(self):
        super().setup_schedule()
        for name in WORKER_HEALTH_TASK_GROUPS:
            entry = self.schedule[name]
            entry.last_run_at = entry.default_now() - entry.schedule.run_every


app.conf.beat_scheduler = "config.celery:WorkerHealthFirstScheduler"
