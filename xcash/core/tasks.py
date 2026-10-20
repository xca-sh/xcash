from datetime import timedelta

import structlog
from django.utils import timezone
from procrastinate.contrib.django import app
from procrastinate.jobs import Status

from config.worker_health import record_worker_health
from core.monitoring import OperationalRiskService

logger = structlog.get_logger()

# 回收 stalled 任务时，事件任务延迟多久重新执行：给刚被拉起的进程与依赖留出恢复时间。
STALLED_JOB_RETRY_DELAY_SECONDS = 60
# 事件任务此前已失败或被回收的次数（attempts，含正常重试与 stalled 回收）达到此值仍
# stalled 即判为毒任务，置为 failed：防止一个每次都挂死进程的任务被无限重排、反复打崩 worker。
STALLED_JOB_MAX_ATTEMPTS = 20
# 结束状态（failed / cancelled / aborted）的任务保留天数，供后台「任务日志」排查；
# 成功的任务在完成时即已删除。
FINISHED_JOB_RETENTION_HOURS = 7 * 24


@app.task(name="core.report_worker_health")
def report_worker_health(*, timestamp: int | None = None) -> None:
    """周期探针：worker 实际执行到它时写入心跳。

    timestamp 是周期计划时刻，记为发布时间；执行时刻记为完成时间。它同时证明周期投递在跑
    （投递与执行在同一 worker 进程内）、数据库可写、worker 有空闲执行容量（探针优先级
    最高，不会排在积压后面）。
    """
    record_worker_health(published_at=timestamp)


@app.task(name="core.scan_operational_risks")
def scan_operational_risks(*, timestamp: int | None = None) -> None:  # noqa: ARG001
    """周期性巡检回调链路中的卡单风险，并输出结构化告警。"""
    summary = OperationalRiskService.build_summary(
        limit=3, include_resource_checks=True
    )
    # 每轮都刷新缓存（含清零），badge 等展示入口据此低成本判断资源风险，
    # 不必在页面渲染时实时打多链 RPC。须在下方 early-return 之前写入，否则
    # 风险消失（计数归零）那一轮不会更新缓存，badge 会卡在过期的风险态。
    OperationalRiskService.cache_resource_risk_counts(
        evm_low_native_balance_count=summary["evm_low_native_balance_count"],
        tron_low_resource_count=summary["tron_low_resource_count"],
    )
    risk_count = (
        summary["stalled_webhook_event_count"]
        + summary["evm_low_native_balance_count"]
        + summary["tron_low_resource_count"]
        + summary["stale_price_crypto_count"]
    )
    if not risk_count:
        return

    logger.warning(
        "运营巡检发现异常任务",
        stalled_webhook_events=summary["stalled_webhook_event_count"],
        evm_low_native_balances=summary["evm_low_native_balance_count"],
        tron_low_resources=summary["tron_low_resource_count"],
        # 价格陈旧会让账单按错误汇率收款，必须与卡单风险同级告警。
        stale_price_cryptos=summary["stale_price_crypto_count"],
        sample_stale_price_symbols=[
            item["symbol"] for item in summary["recent_stale_price_cryptos"]
        ],
        sample_event_ids=[
            event.pk for event in summary["recent_stalled_webhook_events"]
        ],
        sample_evm_senders=[
            alert["sender"].address
            for alert in summary["recent_evm_low_native_balance_alerts"]
            if alert.get("sender") is not None
        ],
        sample_tron_senders=[
            alert["sender"].address
            for alert in summary["recent_tron_low_resource_alerts"]
            if alert.get("sender") is not None
        ],
    )


@app.task(name="core.retry_stalled_jobs", pass_context=True)
async def retry_stalled_jobs(
    context, *, timestamp: int | None = None
) -> None:  # noqa: ARG001
    """回收所属 worker 已失联（被看门狗退出、OOM、SIGKILL）的 doing 任务。

    Procrastinate 只登记心跳、不会自动重试这类任务，它们会永远停在 doing：
    - 周期任务：直接置为 failed。目的是释放执行锁（lock），下个周期照常执行，不需要补跑。
    - 事件任务：延迟 STALLED_JOB_RETRY_DELAY_SECONDS 重新排队（at-least-once，任务均幂等）；
      attempts 已达 STALLED_JOB_MAX_ATTEMPTS 的视为毒任务，置为 failed 并告警。
    - 重新排队时同一 queueing_lock 已有新的待执行任务（期间被别处重新入队）：新任务会做
      同样的事，由 worker 的 WorkerJobManager.retry_job 把旧任务置为 failed。

    本任务只设 queueing_lock、不设 lock：它自己 stalled 时若带 lock，卡在 doing 的那个
    实例会挡住后续全部实例，回收机制自身就死锁了。
    """
    job_manager = context.app.job_manager
    periodic_task_names = {
        periodic_task.task.name
        for periodic_task in context.app.periodic_registry.periodic_tasks.values()
    }
    for job in await job_manager.get_stalled_jobs():
        log = logger.bind(job_id=job.id, task=job.task_name, attempts=job.attempts)
        try:
            if job.task_name in periodic_task_names:
                await job_manager.finish_job(
                    job=job, status=Status.FAILED, delete_job=False
                )
                log.error("周期任务所属 worker 已失联，置为失败以释放执行锁")
                continue
            if job.attempts >= STALLED_JOB_MAX_ATTEMPTS:
                await job_manager.finish_job(
                    job=job, status=Status.FAILED, delete_job=False
                )
                log.error("任务反复 stalled，判为毒任务置为失败，需人工排查")
                continue
            # 先记再重排：同键已有待执行任务接替时，retry_job 改为置失败并另记一条。
            log.error("任务所属 worker 已失联，延迟重新排队")
            await job_manager.retry_job(
                job=job,
                retry_at=timezone.now()
                + timedelta(seconds=STALLED_JOB_RETRY_DELAY_SECONDS),
            )
        except Exception:
            # 单个任务回收失败（如期间已被别处收口）不影响其余任务，下一轮再看。
            log.exception("回收 stalled 任务失败")


@app.task(name="core.cleanup_finished_jobs", pass_context=True)
async def cleanup_finished_jobs(
    context, *, timestamp: int | None = None
) -> None:  # noqa: ARG001
    """删除结束超过保留期的 failed / cancelled / aborted 任务（状态事件随之级联删除）。"""
    await context.app.job_manager.delete_old_jobs(
        nb_hours=FINISHED_JOB_RETENTION_HOURS,
        include_failed=True,
        include_cancelled=True,
        include_aborted=True,
    )
