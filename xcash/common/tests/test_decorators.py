from unittest.mock import patch

from django.test import SimpleTestCase

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
