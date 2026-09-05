"""集成测试的独立 worker：任务只写测试回执，不加载或执行实际业务。"""

import json
import sys

from celery import Celery
from redis import Redis

from config.celery import subscribe_periodic_queues  # noqa: F401 - 注册真实启动信号
from config.periodic_tasks import PERIODIC_TASK_QUEUES


def main():
    config = json.load(sys.stdin)
    app = Celery("periodic-queue-worker-test", set_as_current=False, fixups=[])
    app.conf.update(config["celery"])
    client = Redis.from_url(
        app.conf.broker_url,
        socket_timeout=3,
        socket_connect_timeout=3,
    )

    def record_tick(task):
        client.rpush(config["receipt_key"], task.name)

    for name in PERIODIC_TASK_QUEUES:
        app.task(
            name=name,
            ignore_result=True,
            bind=True,
            shared=False,
            lazy=False,
        )(record_tick)
    app.Worker(
        queues=[config["group"]],
        hostname=config["hostname"],
        pool="solo",
        concurrency=1,
        without_gossip=True,
        without_mingle=True,
        without_heartbeat=True,
        loglevel="WARNING",
    ).start()


if __name__ == "__main__":
    main()
