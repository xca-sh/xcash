"""由真实执行形成存活心跳；HTTP 只读缓存，不发布任务或调用 inspect。

两类心跳共用一套判定：
- celery 业务 worker：Beat 投递探针、worker 实际消费后写入，同时证明调度与消费都在跑。
- chain-runner：常驻扫描进程的 Supervisor 每个 tick 直接写入，证明进程存活、DB 与
  advisory lock 连接可用；没有活跃链时同样成立。链级扫描是否停摆由 /health/scanning 判定。
"""

import math
import time

import structlog
from django.core.cache import cache

from config.periodic_tasks import WORKER_HEALTH_TASK_GROUPS

WORKER_HEALTH_INTERVAL_SECONDS = 30
# 业务 worker 可能短暂满载；覆盖最长生产任务 290s 加下一轮调度，避免正常执行误报。
WORKER_HEALTH_MAX_AGE_SECONDS = 360
CHAIN_RUNNER_HEALTH_GROUP = "chain-runner"
HEALTH_GROUPS = tuple(
    sorted({*WORKER_HEALTH_TASK_GROUPS.values(), CHAIN_RUNNER_HEALTH_GROUP})
)
WORKER_HEALTH_PUBLISHED_HEADER = "xcash_health_published_at"
logger = structlog.get_logger()


def stamp_worker_health_probe(sender=None, headers=None, **kwargs):  # noqa: ARG001
    """每次发布记录真实时刻，重启后消费旧积压不能冒充新的 Beat 调度。"""
    if sender in WORKER_HEALTH_TASK_GROUPS and headers is not None:
        headers[WORKER_HEALTH_PUBLISHED_HEADER] = time.time()


def worker_health_key(group):
    return f"health:worker:{group}"


def record_worker_health(task):
    group = WORKER_HEALTH_TASK_GROUPS[task.name]
    published_at = (task.request.headers or {}).get(WORKER_HEALTH_PUBLISHED_HEADER)
    cache.set(
        worker_health_key(group),
        {"published_at": published_at, "completed_at": time.time()},
        timeout=WORKER_HEALTH_MAX_AGE_SECONDS,
    )


def record_chain_runner_heartbeat():
    """chain-runner 没有「发布」环节，发布与完成取同一时刻，沿用同一套新鲜度判定。"""
    now = time.time()
    cache.set(
        worker_health_key(CHAIN_RUNNER_HEALTH_GROUP),
        {"published_at": now, "completed_at": now},
        timeout=WORKER_HEALTH_MAX_AGE_SECONDS,
    )


def stale_worker_groups(*, published_after=0, groups=HEALTH_GROUPS):
    """缺失、过期、损坏或未来时间都失败关闭；发布与执行均需处于健康窗口内。"""
    samples = cache.get_many([worker_health_key(group) for group in groups])
    now = time.time()
    stale = []
    for group in groups:
        sample = samples.get(worker_health_key(group))
        if not isinstance(sample, dict):
            stale.append(group)
            continue
        timestamps = [sample.get("published_at"), sample.get("completed_at")]
        if (
            not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and now - WORKER_HEALTH_MAX_AGE_SECONDS <= value <= now
                for value in timestamps
            )
            or timestamps[0] < published_after
        ):
            stale.append(group)
    return stale


def worker_health_status():
    """HTTP 与后台展示共用状态；无法读取心跳必须作为风险，不能回落成健康。"""
    try:
        stale = stale_worker_groups()
    except Exception:
        logger.warning("worker_health_unavailable", exc_info=True)
        return {"status": "unhealthy", "groups": [], "risk_count": 1}
    return {
        "status": "stalled" if stale else "ok",
        "groups": stale,
        "risk_count": len(stale),
    }
