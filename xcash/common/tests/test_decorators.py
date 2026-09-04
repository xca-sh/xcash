from unittest.mock import patch

from celery import Celery
from celery import Task
from django.test import SimpleTestCase

from common.decorators import PENDING_ONCE_ACQUIRE_SCRIPT
from common.decorators import PENDING_ONCE_PROMOTE_SCRIPT
from common.decorators import PENDING_ONCE_RELEASE_SCRIPT
from common.decorators import PendingOnceTask
from common.decorators import singleton_task


class FakeCache:
    def __init__(self):
        self.values = {}
        self.last_key = ""
        self.deleted_keys = []

    def add(self, key, value, timeout):
        if key in self.values:
            return False
        self.values[key] = value
        self.last_key = key
        return True

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        self.deleted_keys.append(key)
        self.values.pop(key, None)


class FakePendingOnceRedis:
    """按生产 Lua 的状态机模拟 Redis，测试 producer/worker 间的所有权竞态。"""

    def __init__(self):
        self.markers = {}
        self.now_ms = 1_000_000
        self.fail_next_script = None

    def advance(self, milliseconds):
        self.now_ms += milliseconds

    def fail_next(self, script):
        self.fail_next_script = script

    def eval(self, script, key_count, state_key, field, task_id, *lease_values):
        if key_count != 1:
            raise AssertionError("测试只支持一个 Redis key")
        if self.fail_next_script == script:
            self.fail_next_script = None
            raise ConnectionError("redis unavailable")

        marker_key = (state_key, field)
        marker = self.markers.get(marker_key)
        if script == PENDING_ONCE_ACQUIRE_SCRIPT:
            publishing_lease_ms, pending_lease_ms = lease_values
            if marker is not None:
                lease_ms = (
                    publishing_lease_ms
                    if marker["state"] == "publishing"
                    else pending_lease_ms
                )
                if self.now_ms - marker["updated_at_ms"] < lease_ms:
                    return [0, marker["task_id"].encode()]
            self.markers[marker_key] = {
                "state": "publishing",
                "task_id": task_id,
                "updated_at_ms": self.now_ms,
            }
            return [1, task_id.encode()]

        if script == PENDING_ONCE_PROMOTE_SCRIPT:
            if marker is None or marker["task_id"] != task_id:
                return 0
            marker.update(state="pending", updated_at_ms=self.now_ms)
            return 1

        if script == PENDING_ONCE_RELEASE_SCRIPT:
            if marker is None or marker["task_id"] != task_id:
                return 0
            del self.markers[marker_key]
            return 1

        raise AssertionError("收到未知 Lua 脚本")


class ExamplePendingOnceTask(PendingOnceTask):
    name = "common.tests.example_pending_once"

    def run(self):
        return "ok"


class LifecyclePendingOnceTask(PendingOnceTask):
    name = "common.tests.lifecycle_pending_once"
    publish_during_run = False

    def run(self):
        if self.publish_during_run:
            self.apply_async(task_id="task-2")
        return "ok"


class PendingOnceTaskTests(SimpleTestCase):
    def setUp(self):
        self.redis = FakePendingOnceRedis()
        self.task = ExamplePendingOnceTask()
        self.redis_patch = patch(
            "common.decorators.get_redis_connection",
            return_value=self.redis,
        )
        self.redis_patch.start()
        self.addCleanup(self.redis_patch.stop)

    def test_duplicate_publish_only_writes_to_broker_once(self):
        with patch.object(
            Task,
            "apply_async",
            autospec=True,
            return_value="published",
        ) as publish:
            first = self.task.apply_async(task_id="task-1", queue="scan")
            duplicate = self.task.apply_async(task_id="task-2", queue="scan")

        self.assertEqual(first, "published")
        self.assertEqual(duplicate.id, "task-1")
        publish.assert_called_once()
        self.assertEqual(publish.call_args.kwargs["task_id"], "task-1")
        self.assertEqual(publish.call_args.kwargs["queue"], "scan")

    def test_before_start_releases_pending_and_allows_next_publish(self):
        with patch.object(Task, "apply_async", autospec=True, return_value="published"):
            self.task.apply_async(task_id="task-1")
            self.task.before_start("task-1", (), {})
            self.task.apply_async(task_id="task-2")

        marker = next(iter(self.redis.markers.values()))
        self.assertEqual(marker["task_id"], "task-2")
        self.assertEqual(marker["state"], "pending")

    def test_publish_failure_releases_own_marker(self):
        with (
            patch.object(
                Task,
                "apply_async",
                autospec=True,
                side_effect=RuntimeError("publish failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "publish failed"),
        ):
            self.task.apply_async(task_id="task-1")

        self.assertEqual(self.redis.markers, {})

    def test_redis_acquire_failure_does_not_publish(self):
        self.redis.fail_next(PENDING_ONCE_ACQUIRE_SCRIPT)
        with (
            patch.object(Task, "apply_async", autospec=True) as publish,
            self.assertRaisesRegex(ConnectionError, "redis unavailable"),
        ):
            self.task.apply_async(task_id="task-1")

        publish.assert_not_called()

    def test_old_after_return_cannot_delete_new_owner(self):
        with patch.object(Task, "apply_async", autospec=True, return_value="published"):
            self.task.apply_async(task_id="task-1")
            self.task.before_start("task-1", (), {})
            self.task.apply_async(task_id="task-2")
            self.task.after_return("SUCCESS", None, "task-1", (), {}, None)

        marker = next(iter(self.redis.markers.values()))
        self.assertEqual(marker["task_id"], "task-2")

    def test_celery_lifecycle_releases_before_run_without_deleting_next_owner(self):
        app = Celery("pending-once-lifecycle-test")
        task = app.register_task(LifecyclePendingOnceTask())
        task.publish_during_run = True
        with patch.object(
            Task,
            "apply_async",
            autospec=True,
            return_value="published",
        ) as publish:
            task.apply_async(task_id="task-1")
            result = task.apply(task_id="task-1", throw=True)
            duplicate = task.apply_async(task_id="task-3")

        self.assertEqual(result.result, "ok")
        self.assertEqual(duplicate.id, "task-2")
        self.assertEqual(publish.call_count, 2)
        marker = next(iter(self.redis.markers.values()))
        self.assertEqual(marker["task_id"], "task-2")

    def test_fast_worker_does_not_let_promote_restore_released_marker(self):
        def publish_and_start(task, *args, **kwargs):  # noqa: ARG001
            task.before_start(
                kwargs["task_id"],
                kwargs["args"],
                kwargs["kwargs"],
            )
            return "published"

        with patch.object(
            Task,
            "apply_async",
            autospec=True,
            side_effect=publish_and_start,
        ):
            result = self.task.apply_async(task_id="task-1")

        self.assertEqual(result, "published")
        self.assertEqual(self.redis.markers, {})

    def test_stale_publishing_and_pending_markers_can_be_taken_over(self):
        with patch.object(Task, "apply_async", autospec=True, return_value="published"):
            self.redis.fail_next(PENDING_ONCE_PROMOTE_SCRIPT)
            self.task.apply_async(task_id="publishing-owner")
            self.redis.advance(self.task.pending_once_publishing_lease_ms)
            self.task.apply_async(task_id="pending-owner")
            self.redis.advance(self.task.pending_once_pending_lease_ms)
            self.task.apply_async(task_id="replacement-owner")

        marker = next(iter(self.redis.markers.values()))
        self.assertEqual(marker["task_id"], "replacement-owner")
        self.assertEqual(marker["state"], "pending")

    def test_promote_failure_does_not_report_publish_failure(self):
        self.redis.fail_next(PENDING_ONCE_PROMOTE_SCRIPT)
        with patch.object(
            Task,
            "apply_async",
            autospec=True,
            return_value="published",
        ) as publish:
            result = self.task.apply_async(task_id="task-1")

        self.assertEqual(result, "published")
        publish.assert_called_once()

    def test_rejects_parameterized_calls_before_publishing(self):
        with patch.object(Task, "apply_async", autospec=True) as publish:
            with self.assertRaisesRegex(TypeError, "仅允许用于无参数周期任务"):
                self.task.apply_async(args=(1,))
            with self.assertRaisesRegex(TypeError, "仅允许用于无参数周期任务"):
                self.task.apply_async(kwargs={"chain_pk": 1})

        publish.assert_not_called()


class SingletonTaskTests(SimpleTestCase):
    def test_finally_does_not_delete_lock_owned_by_new_instance(self):
        fake_cache = FakeCache()

        @singleton_task(timeout=5)
        def task():
            fake_cache.values[fake_cache.last_key] = "new-owner"
            return "ok"

        with patch("common.decorators.cache", fake_cache):
            result = task()

        self.assertEqual(result, "ok")
        self.assertEqual(
            fake_cache.values[f"{task.__name__}-locked"],
            "new-owner",
        )
        self.assertEqual(fake_cache.deleted_keys, [])

    def test_finally_deletes_own_lock(self):
        fake_cache = FakeCache()

        @singleton_task(timeout=5)
        def task():
            return "ok"

        with patch("common.decorators.cache", fake_cache):
            result = task()

        self.assertEqual(result, "ok")
        self.assertNotIn(f"{task.__name__}-locked", fake_cache.values)
        self.assertEqual(fake_cache.deleted_keys, [f"{task.__name__}-locked"])

    def test_bound_task_instance_does_not_affect_lock_key(self):
        """bind=True 任务的 Task 实例不能参与锁 key 计算。

        Celery 传给 bind 任务的第一个位置参数是 Task 实例，其 repr 尾部带 id(app)
        内存地址，逐 worker 进程不同。若它进了 key，同一任务同参数会在每个进程各算
        出一把锁，跨进程互斥彻底失效——同一笔 Transfer 会被多个 worker 同时确认。
        """
        from celery import Celery

        # 两个 app 必须同时存活：若先建后弃，CPython 可能把同一内存地址分配给第二个
        # app，repr 恰好相同，测试就会假通过。
        apps = [Celery("xcash-test"), Celery("xcash-test")]
        bound_tasks = []
        for app in apps:

            @app.task(bind=True)
            def bound_task(self, pk):  # noqa: ARG001
                return "ok"

            bound_tasks.append(bound_task)

        # 前提校验：两个 Task 实例的 repr 确实不同（含各自 app 的内存地址），
        # 否则本测试无法证明"剔除 Task 实例"这件事。
        self.assertNotEqual(repr(bound_tasks[0]), repr(bound_tasks[1]))

        @singleton_task(timeout=5, use_params=True)
        def wrapped(task_self, pk):  # noqa: ARG001
            return "ok"

        keys = []
        for task_instance in bound_tasks:
            fake_cache = FakeCache()
            with patch("common.decorators.cache", fake_cache):
                wrapped(task_instance, 7)
            keys.append(fake_cache.last_key)

        self.assertEqual(keys[0], keys[1])

    def test_lock_key_still_separates_different_params(self):
        """剔除 Task 实例后，普通参数仍必须区分出不同的锁。"""
        fake_cache = FakeCache()

        @singleton_task(timeout=5, use_params=True)
        def task(pk):  # noqa: ARG001
            return "ok"

        with patch("common.decorators.cache", fake_cache):
            task(1)
            first_key = fake_cache.last_key
            task(2)
            second_key = fake_cache.last_key

        self.assertNotEqual(first_key, second_key)
