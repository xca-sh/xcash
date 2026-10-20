"""发布阶段的一次性就绪门控，不把常驻探测开销放进 HTTP 健康探针。"""

import time

import httpx
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from config.worker_health import stale_worker_groups


def check_http_health(client, url, timeout):
    response = client.get(url, timeout=timeout)
    response.raise_for_status()
    if response.json() != {"status": "ok"}:
        raise ValueError("HTTP health response is not ready")


class Command(BaseCommand):
    help = "等待 HTTP 恢复，并等待新启动的 worker 与 chain-runner 产生心跳。"
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=int, default=360)
        parser.add_argument("--url", default="http://xcash-caddy/health")
        parser.add_argument("--http-stopped-at", type=float)
        # 新进程可能早于本命令开始就写下心跳；调用方传入发起启动的时刻作为就绪下限，
        # 未传时取本命令开始时刻。
        parser.add_argument("--started-after", type=float)

    def handle(self, *args, **options):
        timeout = options["timeout"]
        if timeout <= 0:
            raise CommandError("timeout must be positive")
        started_after = options["started_after"]
        if started_after is None:
            started_after = time.time()
        started = time.monotonic()
        deadline = started + timeout
        last_error = "runtime has not responded"
        reported_error = None
        reported_at = started
        http_reported = False
        self.stdout.write(f"waiting for runtime (timeout {timeout}s)")
        self.stdout.flush()
        # 内网请求不得继承宿主代理；不跟随重定向，避免入口路由错误被最终 200 掩盖。
        with httpx.Client(trust_env=False, follow_redirects=False) as client:
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    check_http_health(client, options["url"], min(5, remaining))
                    if not http_reported:
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
                    # 只认启动下限之后执行的心跳：旧进程停止前留在缓存里的那条不能冒充
                    # 新进程已就绪。worker 心跳来自周期探针，首个探针最迟一个周期（30s）出现。
                    missing = stale_worker_groups(completed_after=started_after)
                    if not missing:
                        self.stdout.write(
                            f"runtime ready in {time.monotonic() - started:.1f}s"
                        )
                        self.stdout.flush()
                        return
                    last_error = f"waiting for heartbeats: {', '.join(missing)}"
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
