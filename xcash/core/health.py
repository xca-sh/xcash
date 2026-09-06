"""存活探测端点。

/health 只回答一个问题：本进程现在还能不能正常服务请求——即它的两个硬依赖
（Postgres、Redis）是否可用。业务层面的异常巡检是 core/monitoring.py 的职责，
判据一律定义在那里，本模块只提供 HTTP 入口：健康探测被高频调用（默认 30s 一次），
必须恒定廉价。

/health/scanning 与 /health/workers 探测其他进程是否仍在干活。任务执行产生的
事实由 Web 独立检查是否过期，告警不能依赖故障 worker 自己运行巡检。

安全约束：该端点无鉴权（容器内探测无法携带商户签名），因此响应体只允许出现
status 字段。绝不返回版本号、依赖拓扑、异常堆栈等信息——那会把内部结构白送给
扫描者。失败细节只写进结构化日志，由部署方的日志链路查看。
"""

from __future__ import annotations

import structlog
from django.core.cache import cache
from django.db import connection
from django.db import transaction
from django.http import HttpRequest
from django.http import JsonResponse

from config.worker_health import worker_health_status
from core.monitoring import SCAN_STALL_ALERT_AFTER_SECONDS
from core.monitoring import OperationalRiskService

logger = structlog.get_logger()

# 探测键写入缓存后立即读回，用于验证 Redis 的读写双向可用（只连上不代表能用，
# 典型如 maxmemory 打满且无可淘汰键时写入会被拒绝）。TTL 取小值，探测键无需留存。
HEALTH_PROBE_CACHE_KEY = "health:probe"
HEALTH_PROBE_CACHE_TTL = 30


@transaction.non_atomic_requests
def workers_health_view(request: HttpRequest) -> JsonResponse:
    """两组 worker 都需有新鲜的调度与执行心跳；依赖错误同样返回 503。"""
    health = worker_health_status()
    if health["status"] == "stalled":
        logger.warning("workers_probe_stalled", groups=health["groups"])
    return JsonResponse(
        {"status": health["status"]},
        status=200 if health["status"] == "ok" else 503,
    )


@transaction.non_atomic_requests
def health_view(request: HttpRequest) -> JsonResponse:
    """返回 200 表示依赖健康，503 表示至少一个硬依赖不可用。

    用 non_atomic_requests 显式退出 ATOMIC_REQUESTS：探测是纯只读的，
    没必要为每次探测开启一个写事务，也避免 DB 变慢时探测长时间持有事务。
    """
    unhealthy = []

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        # 探测失败是预期内的运行状态而非代码缺陷，记 warning 并继续检查下一项，
        # 使响应能一次性反映"哪些依赖挂了"，而不是在第一个失败处中断。
        logger.warning("health_probe_database_unavailable", exc_info=True)
        unhealthy.append("database")

    cache_healthy = False
    try:
        cache.set(HEALTH_PROBE_CACHE_KEY, "1", timeout=HEALTH_PROBE_CACHE_TTL)
        cache_healthy = cache.get(HEALTH_PROBE_CACHE_KEY) == "1"
        if not cache_healthy:
            logger.warning("health_probe_cache_readback_mismatch")
    except Exception:
        logger.warning("health_probe_cache_unavailable", exc_info=True)

    if not cache_healthy:
        unhealthy.append("cache")

    if unhealthy:
        logger.warning("health_probe_unhealthy", components=unhealthy)
        return JsonResponse({"status": "unhealthy"}, status=503)

    return JsonResponse({"status": "ok"})


@transaction.non_atomic_requests
def scanning_health_view(request: HttpRequest) -> JsonResponse:
    """扫描存活探测：503 表示至少一条活跃链的扫描不健康。

    两条判据缺一不可，覆盖两种彼此独立的失效：
    - stalled：扫描任务【根本没被执行】（worker 死亡、beat 停摆、队列积压、
      broker 不可写）。
    - failing：扫描任务【在跑但一直失败】（RPC 凭据失效、节点持续报错、每轮撞
      软超时）。这类故障下 Chain.last_scanned_at 仍被 finally 分支照常推进，
      只看 stalled 会一路报健康，而实际游标和入账完全不动。

    有意与 /health 分开，不能合并：
    - /health 是 django 容器自己的 healthcheck。扫描停摆时 django 进程本身完全
      健康，把它并进去会让 django 被误判为不健康。
    - 本端点必须由【django 进程】而非 Celery 任务回答。既有的 scan_operational_risks
      巡检跑在 worker 里，worker 一死它跟着一起死，巡检和故障对象是同一个进程，
      扫描停摆时不会有任何人被告知。本端点给外部监控（uptime 探针）拉取，
      故障域与被监控对象天然分离。

    安全约束同 /health：无鉴权，响应体只出现 status，问题链的细节只写结构化日志。
    """
    stalled = OperationalRiskService.stalled_scan_chains()
    failing = OperationalRiskService.failing_scan_chains()
    if stalled or failing:
        logger.warning(
            "scanning_probe_unhealthy",
            stall_after_seconds=SCAN_STALL_ALERT_AFTER_SECONDS,
            stalled_chains=[chain.code for chain in stalled],
            failing_chains=[chain.code for chain in failing],
        )
        return JsonResponse({"status": "stalled"}, status=503)

    return JsonResponse({"status": "ok"})
