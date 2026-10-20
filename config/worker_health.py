"""由真实执行形成存活心跳；HTTP 只读缓存，不发布任务、不查询进程。

两类心跳共用一套判定：
- worker（业务任务进程）：周期探针 core.report_worker_health 由 worker 内的周期投递产生、
  由 worker 实际执行后写入，同时证明周期投递、数据库与执行容量都正常。
- chain-runner：常驻扫描进程的 Supervisor 每个 tick 直接写入，证明进程存活、DB 与
  advisory lock 连接可用；没有活跃链时同样成立。链级扫描是否停摆由 /health/scanning 判定。
"""

import math
import time

import structlog
from django.core.cache import cache

# 业务 worker 可能短暂满载；覆盖看门狗硬上限 300s 加一个探针周期，避免正常执行误报。
WORKER_HEALTH_MAX_AGE_SECONDS = 360
WORKER_HEALTH_GROUP = "worker"
CHAIN_RUNNER_HEALTH_GROUP = "chain-runner"
HEALTH_GROUPS = (CHAIN_RUNNER_HEALTH_GROUP, WORKER_HEALTH_GROUP)
logger = structlog.get_logger()


def worker_health_key(group):
    return f"health:worker:{group}"


def record_worker_health(*, published_at):
    """worker 探针执行时写入：published_at 是周期计划时刻，completed_at 是执行时刻。"""
    cache.set(
        worker_health_key(WORKER_HEALTH_GROUP),
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


def stale_worker_groups(*, completed_after=0, groups=HEALTH_GROUPS):
    """缺失、过期、损坏或未来时间都失败关闭；发布与执行均需处于健康窗口内。

    completed_after 是发布阶段的就绪下限：执行时刻必须晚于它。只看执行时刻：新 worker
    可能先执行升级前留在队列里的探针，那条探针的计划时刻早于下限，但执行确实出自新进程。
    """
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
            or timestamps[1] < completed_after
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
