"""隔离集成测试进程：加载真实心跳任务，仅运行测试前缀内的 worker 或 Beat。"""

import json
import sys
import time

import django
from django.test import override_settings
from redis import Redis


def main():
    config = json.load(sys.stdin)
    django.setup()
    from config.celery import app  # noqa: PLC0415
    from config.periodic_tasks import WORKER_HEALTH_TASK_GROUPS  # noqa: PLC0415

    app.conf.update(config["celery"])
    app.loader.import_default_modules()
    # 测试 Beat 只投递无业务副作用的两种真实心跳。
    app.conf.beat_schedule = {
        name: {"task": name, "schedule": 0.5} for name in WORKER_HEALTH_TASK_GROUPS
    }
    with override_settings(CACHES=config["caches"]):
        if config["mode"] == "beat":
            app.Beat(schedule=config["schedule"], loglevel="WARNING").run()
            return

        @app.task(name="runtime_test.slow", ignore_result=True)
        def slow():
            with Redis.from_url(app.conf.broker_url, socket_timeout=3) as client:
                client.rpush(config["receipt_key"], "started")
                time.sleep(12)
                client.rpush(config["receipt_key"], "completed")

        app.Worker(
            queues=[config["group"]],
            hostname=config["hostname"],
            pool="prefork",
            concurrency=1,
            without_gossip=True,
            without_mingle=True,
            without_heartbeat=True,
            loglevel="WARNING",
        ).start()


if __name__ == "__main__":
    main()
