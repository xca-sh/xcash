"""使用独立 Redis 前缀验证真实 Celery/Kombu 发布与重投，不碰业务队列。"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from celery import Celery
from celery.beat import ScheduleEntry
from celery.beat import Scheduler
from kombu import Producer
from kombu.exceptions import OperationalError
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import OutOfMemoryError

from config.celery import subscribe_periodic_queues
from config.periodic_tasks import PERIODIC_TASK_GROUPS
from config.periodic_tasks import PERIODIC_TASK_QUEUES

SCAN_TASK = "evm.tasks.scan_active_evm_chains"
BUSINESS_TASK = "evm.tasks.dispatch_evm_tx_tasks"


@pytest.fixture
def broker(settings):
    prefix = f"xcash:test:periodic-queue:{uuid4().hex}:"
    app = Celery("periodic-queue-test", set_as_current=False, fixups=[])
    app.conf.update(
        broker_url=settings.CELERY_BROKER_URL,
        broker_transport=settings.CELERY_BROKER_TRANSPORT,
        broker_transport_options={
            "global_keyprefix": prefix,
            "socket_connect_timeout": 3,
            "socket_timeout": 3,
        },
        task_routes=settings.CELERY_TASK_ROUTES,
        task_serializer="json",
        task_ignore_result=True,
        task_publish_retry=False,
        result_backend="cache+memory://",
    )

    def tick():
        pass

    for name in PERIODIC_TASK_QUEUES:
        app.task(name=name, ignore_result=True, shared=False, lazy=False)(tick)
    client = Redis.from_url(
        settings.CELERY_BROKER_URL,
        socket_timeout=3,
        socket_connect_timeout=3,
    )
    try:
        with app.connection_for_write() as connection:
            channel = connection.default_channel
            yield SimpleNamespace(
                app=app,
                client=client,
                prefix=prefix,
                channel=channel,
            )
    finally:
        app.producer_pool.force_close_all()
        app.close()
        keys = list(client.scan_iter(match=f"{prefix}*"))
        if keys:
            client.delete(*keys)
        client.close()


def queue_size(broker, task=SCAN_TASK):
    return broker.channel.queue_declare(
        PERIODIC_TASK_QUEUES[task],
    ).message_count


def take(broker, task=SCAN_TASK):
    return broker.channel.basic_get(PERIODIC_TASK_QUEUES[task])


def test_concurrent_publish_coalesces_across_priority_buckets(broker):
    task = broker.app.tasks[SCAN_TASK]
    # 第一个 tick 保留，即使后续 producer 使用不同优先级也不能多出消息。
    first = task.apply_async(task_id="first", priority=9)
    assert first.id == "first"

    def publish(index):
        return task.apply_async(task_id=f"duplicate-{index}", priority=index % 10)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(publish, range(200)))
    assert queue_size(broker) == 1
    message = take(broker)
    assert message.headers["id"] == "first"
    message.ack()
    task.delay()
    assert queue_size(broker) == 1


def test_concurrent_publish_to_empty_queue_has_one_winner(broker):
    task = broker.app.tasks[SCAN_TASK]
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: task.delay(), range(100)))
    assert queue_size(broker) == 1


def test_beat_stopped_worker_keeps_one_message_per_entry(broker):
    scheduler = Scheduler(app=broker.app, lazy=True)
    entries = [
        ScheduleEntry(name=name, task=name, schedule=2, app=broker.app)
        for name in PERIODIC_TASK_QUEUES
    ]
    with broker.app.producer_or_acquire() as producer:
        for _ in range(60):
            for entry in entries:
                scheduler.apply_async(entry, producer=producer)
    assert [queue_size(broker, name) for name in PERIODIC_TASK_QUEUES] == [1] * 5
    # 共享业务队列仍可逐条保存带参数消息，不能跟着合并。
    for index in range(10):
        broker.app.send_task("evm.tasks._scan_evm_chain", args=[index])
        broker.app.send_task("webhooks.tasks.deliver", args=[index])
    assert broker.channel.queue_declare("scan").message_count == 10
    assert broker.channel.queue_declare("celery").message_count == 10


@pytest.mark.parametrize("restore", ["reject", "shutdown", "visibility"])
@pytest.mark.parametrize("new_tick", [False, True])
def test_unacked_restore_is_coalesced_and_can_recover_empty_queue(
    broker,
    restore,
    new_tick,
):
    task = broker.app.tasks[SCAN_TASK]
    task.apply_async(task_id="old")
    message = take(broker)
    assert queue_size(broker) == 0
    if new_tick:
        task.apply_async(task_id="new", priority=6)
    if restore == "reject":
        message.reject(requeue=True)
    elif restore == "shutdown":
        broker.channel.qos.restore_unacked()
    else:
        broker.channel.client.zadd(
            broker.channel.unacked_index_key,
            {message.delivery_tag: 0},
        )
        broker.channel.qos.restore_visible(interval=1)
    assert queue_size(broker) == 1
    assert broker.channel.client.hlen(broker.channel.unacked_key) == 0
    assert broker.channel.client.zcard(broker.channel.unacked_index_key) == 0
    restored = take(broker)
    assert restored.headers["id"] == ("new" if new_tick else "old")
    if not new_tick:
        assert restored.headers["redelivered"] is True
    restored.ack()
    task.delay()
    assert queue_size(broker) == 1


def test_restore_many_inflight_copies_does_not_build_backlog(broker):
    task = broker.app.tasks[SCAN_TASK]
    for index in range(20):
        task.apply_async(task_id=f"old-{index}")
        take(broker)
    assert broker.channel.client.hlen(broker.channel.unacked_key) == 20
    broker.channel.qos.restore_unacked()
    assert queue_size(broker) == 1
    assert broker.channel.client.hlen(broker.channel.unacked_key) == 0


def test_ordinary_business_messages_keep_requeue_semantics(broker):
    for index in range(2):
        broker.app.send_task("business.event", args=[index], priority=6)
    message = broker.channel.basic_get("celery")
    message.reject(requeue=True)
    assert broker.channel.queue_declare("celery").message_count == 2
    messages = [broker.channel.basic_get("celery") for _ in range(2)]
    assert {message.payload[0][0] for message in messages} == {0, 1}
    for message in messages:
        message.ack()


@pytest.mark.parametrize("written", [False, True])
def test_publish_failure_retry_keeps_queue_bounded(broker, written):
    task = broker.app.tasks[SCAN_TASK]
    original_eval = Redis.eval

    def fail(client, *args):
        if written:
            original_eval(client, *args)
        raise RedisConnectionError("connection lost")

    with (
        patch.object(Redis, "eval", autospec=True, side_effect=fail),
        pytest.raises(OperationalError, match="connection lost"),
    ):
        task.apply_async(task_id="uncertain", producer=Producer(broker.channel))
    assert queue_size(broker) == int(written)
    task.apply_async(task_id="retry")
    assert queue_size(broker) == 1
    message = take(broker)
    assert message.headers["id"] == ("uncertain" if written else "retry")
    message.ack()


def test_redis_oom_does_not_fall_back_to_unguarded_publish(broker):
    with (
        patch.object(Redis, "eval", side_effect=OutOfMemoryError),
        pytest.raises(OperationalError) as raised,
    ):
        broker.app.tasks[SCAN_TASK].apply_async(producer=Producer(broker.channel))
    assert isinstance(raised.value.__cause__, OutOfMemoryError)
    assert queue_size(broker) == 0
    broker.app.tasks[SCAN_TASK].delay()
    assert queue_size(broker) == 1


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ({"args": [1]}, TypeError),
        ({"kwargs": {"chain_id": 1}}, TypeError),
        ({"queue": "scan"}, ValueError),
        ({"countdown": 30}, ValueError),
        ({"expires": 30}, ValueError),
        ({"link": {"task": "business.next"}}, ValueError),
        ({"link_error": {"task": "business.error"}}, ValueError),
        ({"chain": [{"task": "business.next"}]}, ValueError),
        ({"group_id": "group"}, ValueError),
        ({"ignore_result": False}, ValueError),
        ({"serializer": "pickle"}, ValueError),
    ],
)
def test_incompatible_periodic_calls_fail_before_queue_write(broker, options, error):
    with pytest.raises(error):
        broker.app.tasks[SCAN_TASK].apply_async(**options)
    assert queue_size(broker) == 0


def test_send_task_cannot_bypass_dedup_or_merge_parameterized_work(broker):
    for _ in range(20):
        broker.app.send_task(SCAN_TASK, ignore_result=True)
    assert queue_size(broker) == 1
    with pytest.raises(TypeError, match="参数"):
        broker.app.send_task(SCAN_TASK, args=[1], ignore_result=True)
    with pytest.raises(ValueError, match="其他任务"):
        broker.app.send_task("business.event", queue=PERIODIC_TASK_QUEUES[SCAN_TASK])
    assert queue_size(broker) == 1


def test_compressed_json_tick_can_be_published_and_consumed(broker):
    broker.app.tasks[SCAN_TASK].apply_async(compression="gzip")
    broker.app.tasks[SCAN_TASK].apply_async(compression="gzip")
    assert queue_size(broker) == 1
    message = take(broker)
    assert message.payload[0] == []
    message.ack()


@pytest.mark.parametrize(
    "groups", [("celery",), ("scan",), ("celery", "scan"), ("stress",)]
)
def test_worker_group_subscription_preserves_isolation(broker, groups):
    queues = broker.app.amqp.queues
    queues.select(groups)
    subscribe_periodic_queues(
        sender="test-worker", instance=SimpleNamespace(app=broker.app)
    )
    expected = {
        PERIODIC_TASK_QUEUES[task]
        for task, group in PERIODIC_TASK_GROUPS.items()
        if group in groups
    }
    assert set(queues.consume_from) == set(groups) | expected


def test_global_prefix_is_used_for_every_priority_bucket(broker):
    broker.app.tasks[SCAN_TASK].apply_async(priority=6)
    broker.app.tasks[SCAN_TASK].apply_async(priority=0)
    keys = [
        key
        for key in broker.client.scan_iter(match=f"{broker.prefix}*")
        if broker.client.type(key) == b"list"
    ]
    assert len(keys) == 1
    assert broker.client.llen(keys[0]) == 1
    assert broker.client.ttl(keys[0]) == -1
    assert json.loads(broker.client.lindex(keys[0], 0))["headers"]["task"] == SCAN_TASK


@pytest.mark.parametrize("group", ["celery", "scan"])
def test_real_worker_consumes_its_periodic_group_and_next_tick(broker, tmp_path, group):
    """走生产 Worker 启动顺序和异步 BRPOP，防止只测发布而漏订阅新队列。"""
    own_task = SCAN_TASK if group == "scan" else BUSINESS_TASK
    other_task = BUSINESS_TASK if group == "scan" else SCAN_TASK
    for _ in range(20):
        broker.app.tasks[own_task].delay()
        broker.app.tasks[other_task].delay()
    receipt_key = f"{broker.prefix}receipts"
    config = {
        "celery": {
            key: broker.app.conf[key]
            for key in (
                "broker_url",
                "broker_transport",
                "broker_transport_options",
                "task_routes",
                "task_serializer",
                "task_ignore_result",
                "task_publish_retry",
                "result_backend",
            )
        },
        "group": group,
        "receipt_key": receipt_key,
        "hostname": f"periodic-queue-{uuid4().hex}@localhost",
    }
    package_root = str(Path(__file__).resolve().parents[3])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([package_root, *sys.path])}
    output_path = tmp_path / "worker.log"
    with output_path.open("w") as output:
        process = subprocess.Popen(  # noqa: S603 - 仅启动本仓库的隔离测试 worker
            [sys.executable, "-m", "common.tests.redis_transport_worker"],
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        try:
            process.stdin.write(json.dumps(config))
            process.stdin.close()
            for _ in range(2):
                deadline = monotonic() + 15
                receipt = None
                while monotonic() < deadline and process.poll() is None:
                    receipt = broker.client.blpop(receipt_key, timeout=1)
                    if receipt is not None:
                        break
                assert receipt is not None, output_path.read_text()
                assert receipt[1].decode() == own_task
                assert queue_size(broker, other_task) == 1
                broker.app.tasks[own_task].delay()
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    # worker 停止后反复发布仍只留一条；下一次启动无需等待任何租约。
    for _ in range(20):
        broker.app.tasks[own_task].delay()
    assert queue_size(broker, own_task) == 1


def test_project_registered_entries_really_use_bounded_queues(broker):
    from config.celery import app as project_app

    project_app.loader.import_default_modules()
    producer = Producer(broker.channel)
    for name in PERIODIC_TASK_QUEUES:
        for _ in range(20):
            project_app.tasks[name].apply_async(producer=producer)
        assert queue_size(broker, name) == 1


@pytest.mark.parametrize("scheme", ["redis", "rediss"])
def test_custom_transport_preserves_tls_selected_by_broker_url(scheme):
    # 只构造连接参数，不连接服务器；验证 REDIS_URL 覆盖能实际选择 TLS socket。
    code = """
import ssl
from redis import SSLConnection
from config.celery import app
connection = app.connection_for_write()
channel = object.__new__(connection.transport.Channel)
channel.connection = connection.transport
params = channel._connparams()
secure = issubclass(params['connection_class'], SSLConnection)
assert secure == (connection.hostname == 'tls-test')
if secure:
    assert params['ssl_cert_reqs'] == ssl.CERT_REQUIRED
"""
    host = "tls-test" if scheme == "rediss" else "plain-test"
    result = subprocess.run(  # noqa: S603 - 本地解释器，仅构造连接参数
        [sys.executable, "-c", code],
        env={**os.environ, "REDIS_URL": f"{scheme}://{host}:6379/0"},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
