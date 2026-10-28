"""全部周期任务的唯一登记处，一处看全部周期（cron 按 UTC，第 6 列为秒）。

周期投递由 worker 进程内置的 deferrer 完成，取代 Celery beat。每个周期计划时刻由 PG 唯一
约束 (task_name, periodic_id, defer_timestamp) 保证只投递一次：多开 worker 不会重复投递。
worker 停摆超过 10 分钟期间错过的周期不补投，启动时只补最近一次。

互斥语义：
- queueing_lock=任务名：同一周期任务至多一个待执行实例。上一个还没开始执行时，新周期
  直接跳过（合并），停摆期间不会积压。
- lock=任务名：同一周期任务同时至多一个在执行，取代 singleton_task。卡在 doing 的实例会
  挡住后续实例，由 core.retry_stalled_jobs 回收。
- retry_stalled_jobs 不能加 lock：它自己 stalled 时，带 lock 的那个实例会挡住全部后续实例，
  回收机制自身死锁。健康探针也不加 lock：一次卡住不应让心跳断档。

本模块登记在 PROCRASTINATE_IMPORT_PATHS 中，worker 启动时导入。
"""

from procrastinate.contrib.django import app

from chains.tasks import execute_due_vault_slot_collect_schedules
from chains.tasks import fallback_process_transfer
from chains.tasks import purge_unmatched_dust_transfers_task
from chains.tasks import reap_stale_confirming_transfers
from chains.tasks import reconcile_vault_slot_collect_balance_gaps_task
from common.jobs import PRIORITY_BULK
from common.jobs import PRIORITY_DEFAULT
from common.jobs import PRIORITY_HEALTH
from common.jobs import PRIORITY_URGENT
from core.tasks import cleanup_finished_jobs
from core.tasks import report_worker_health
from core.tasks import retry_stalled_jobs
from core.tasks import scan_operational_risks
from currencies.tasks import refresh_crypto_prices
from evm.tasks import scan_stuck_queued_evm_tx_tasks
from invoices.tasks import fallback_invoice_expired
from webhooks.tasks import reap_stalled_events
from webhooks.tasks import schedule_events


def register_periodic(task, *, cron: str, priority: int, exclusive: bool = True):
    """登记周期任务：queueing_lock 恒为任务名；exclusive 时再加同名执行锁。"""
    app.periodic(
        cron=cron,
        lock=task.name if exclusive else None,
        queueing_lock=task.name,
        priority=priority,
    )(task)


# 每 15 秒：待投递 Webhook 入队；账单过期（过期与支付组合槽位释放的唯一机制）。
register_periodic(schedule_events, cron="* * * * * */15", priority=PRIORITY_DEFAULT)
register_periodic(
    fallback_invoice_expired, cron="* * * * * */15", priority=PRIORITY_DEFAULT
)
# 每 20 秒：兜底派发重试耗尽仍未处理的转账。
register_periodic(
    fallback_process_transfer, cron="* * * * * */20", priority=PRIORITY_DEFAULT
)
# 每 30 秒：worker 健康探针，最高优先级，不能排在积压后面。
register_periodic(
    report_worker_health,
    cron="* * * * * */30",
    priority=PRIORITY_HEALTH,
    exclusive=False,
)
# 每分钟。
register_periodic(refresh_crypto_prices, cron="* * * * *", priority=PRIORITY_DEFAULT)
register_periodic(
    execute_due_vault_slot_collect_schedules, cron="* * * * *", priority=PRIORITY_BULK
)
register_periodic(
    retry_stalled_jobs, cron="* * * * *", priority=PRIORITY_URGENT, exclusive=False
)
# 每 2 分钟。
register_periodic(
    reconcile_vault_slot_collect_balance_gaps_task,
    cron="*/2 * * * *",
    priority=PRIORITY_BULK,
)
register_periodic(scan_operational_risks, cron="*/2 * * * *", priority=PRIORITY_BULK)
# EVM 主动交易卡在 nonce 队首的独立观察者：跑在 worker 里，与 chain-runner 分属不同进程。
register_periodic(
    scan_stuck_queued_evm_tx_tasks, cron="*/2 * * * *", priority=PRIORITY_DEFAULT
)
# 每 5 分钟：强制终结超龄未送达的 Webhook 事件，防止其恒占调度批次头部。
register_periodic(reap_stalled_events, cron="*/5 * * * *", priority=PRIORITY_DEFAULT)
# 每日：清理保留期外的未归类粉尘转账；链上终验并清理超龄 CONFIRMING 转账；
# 清理保留期外的结束任务。
register_periodic(
    purge_unmatched_dust_transfers_task, cron="30 3 * * *", priority=PRIORITY_BULK
)
register_periodic(
    reap_stale_confirming_transfers, cron="30 4 * * *", priority=PRIORITY_BULK
)
register_periodic(cleanup_finished_jobs, cron="0 4 * * *", priority=PRIORITY_BULK)
