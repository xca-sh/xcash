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

    def handle(self, *args, **options):
        timeout = options["timeout"]
        if timeout <= 0:
            raise CommandError("timeout must be positive")
        published_after = time.time()
        deadline = time.monotonic() + timeout
        last_error = "runtime has not responded"
        # 内网请求不得继承宿主代理；不跟随重定向，避免入口路由错误被最终 200 掩盖。
        with httpx.Client(trust_env=False, follow_redirects=False) as client:
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    check_http_health(client, options["url"], min(5, remaining))
                    if options["phase"] == "consumers":
                        missing = missing_consumer_groups(min(2, remaining))
                    else:
                        # 必须是命令开始后发布的探针。旧缓存、旧积压即使刚被消费，
                        # 也不能让一个尚未恢复调度的 Beat 获得成功判定。
                        missing = stale_worker_groups(published_after=published_after)
                    if not missing:
                        self.stdout.write(f"runtime {options['phase']} ready")
                        return
                    last_error = f"waiting for {options['phase']}: {', '.join(missing)}"
                except Exception as exc:
                    # 只输出异常类型；连接 URL 可能含凭据，不能把原始异常带进发布日志。
                    last_error = f"runtime probe failed: {type(exc).__name__}"
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(2, remaining))
        raise CommandError(
            f"runtime readiness timed out after {timeout}s: {last_error}"
        )
