"""发布阶段的一次性就绪门控，不把常驻 inspect 开销放进 HTTP 健康探针。"""

import time

import httpx
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from config.celery import app
from config.periodic_tasks import PERIODIC_TASK_GROUPS
from config.periodic_tasks import PERIODIC_TASK_QUEUES
from config.worker_health import stale_worker_groups


def check_http_health(client, url, timeout):
    response = client.get(url, timeout=timeout)
    response.raise_for_status()
    if response.json() != {"status": "ok"}:
        raise ValueError("HTTP health response is not ready")


def missing_consumer_groups(timeout):
    # 独立连接显式限制网络等待并关闭连接重试；外层轮询统一掌握发布截止时间。
    with app.connection_for_read(
        connect_timeout=timeout,
        transport_options={
            **app.conf.broker_transport_options,
            "socket_connect_timeout": timeout,
            "socket_timeout": timeout,
        },
    ) as connection:
        connection.ensure_connection(max_retries=0)
        replies = app.control.inspect(
            connection=connection,
            timeout=timeout,
        ).active_queues()
    queue_sets = [
        {queue["name"] for queue in queues} for queues in (replies or {}).values()
    ]
    missing = []
    for group in ("celery", "scan"):
        required = {group} | {
            PERIODIC_TASK_QUEUES[task]
            for task, task_group in PERIODIC_TASK_GROUPS.items()
            if task_group == group
        }
        if not any(required <= queues for queues in queue_sets):
            missing.append(group)
    return missing


class Command(BaseCommand):
    help = "等待 HTTP 与队列消费者就绪，或等待本次启动后的真实 Beat 调度回执。"
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument(
            "--phase", choices=("consumers", "scheduler"), required=True
        )
        parser.add_argument("--timeout", type=int, default=360)
        parser.add_argument("--url", default="http://xcash-caddy/health")
        parser.add_argument("--http-stopped-at", type=float)
        # 新 Beat 启动即投递健康探针，可能早于本命令开始；调用方传入发起 Beat 启动的
        # 时刻作为探针发布下限，未传时取本命令开始时刻。
        parser.add_argument("--published-after", type=float)

    def handle(self, *args, **options):
        timeout = options["timeout"]
        if timeout <= 0:
            raise CommandError("timeout must be positive")
        published_after = options["published_after"]
        if published_after is None:
            published_after = time.time()
        started = time.monotonic()
        deadline = started + timeout
        last_error = "runtime has not responded"
        reported_error = None
        reported_at = started
        http_reported = False
        self.stdout.write(
            f"waiting for runtime {options['phase']} (timeout {timeout}s)"
        )
        self.stdout.flush()
        # 内网请求不得继承宿主代理；不跟随重定向，避免入口路由错误被最终 200 掩盖。
        with httpx.Client(trust_env=False, follow_redirects=False) as client:
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    check_http_health(client, options["url"], min(5, remaining))
                    if options["phase"] == "consumers" and not http_reported:
                        message = "HTTP ready through Caddy"
                        if options["http_stopped_at"] is not None:
                            elapsed = max(0, time.time() - options["http_stopped_at"])
                            # 从请求停止到首次成功采样，包含关闭和轮询开销，不是精确故障时长。
                            message += (
                                f"; {elapsed:.1f}s since Django stop was requested"
                            )
                        self.stdout.write(message)
                        self.stdout.flush()
                        http_reported = True
                    if options["phase"] == "consumers":
                        missing = missing_consumer_groups(min(2, remaining))
                    else:
                        # 必须是发布下限之后的探针。旧缓存、旧积压即使刚被消费，
                        # 也不能让一个尚未恢复调度的 Beat 获得成功判定。
                        missing = stale_worker_groups(published_after=published_after)
                    if not missing:
                        self.stdout.write(
                            f"runtime {options['phase']} ready in {time.monotonic() - started:.1f}s"
                        )
                        self.stdout.flush()
                        return
                    last_error = f"waiting for {options['phase']}: {', '.join(missing)}"
                except Exception as exc:
                    # 只输出异常类型；连接 URL 可能含凭据，不能把原始异常带进发布日志。
                    last_error = f"runtime probe failed: {type(exc).__name__}"
                now = time.monotonic()
                # 状态变化立即输出；同一状态最多每十秒输出一次，不泄漏异常中的连接凭据。
                if last_error != reported_error or now - reported_at >= 10:
                    self.stdout.write(f"{last_error} (elapsed {now - started:.1f}s)")
                    self.stdout.flush()
                    reported_error, reported_at = last_error, now
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(2, remaining))
        raise CommandError(
            f"runtime readiness timed out after {timeout}s: {last_error}"
        )
