import json
from functools import wraps
from hashlib import sha256
from uuid import uuid4

from celery import Task
from django.core.cache import cache


def singleton_task(timeout, *, use_params=False):
    """防止同一 Celery 任务并发执行的互斥装饰器。

    通过 Redis cache.add 实现分布式互斥锁：
    - 同一任务（或同参数任务）在执行期间不会被重复执行，后到的直接跳过（返回 None）。
    - 任务正常结束后立即释放锁，不会阻塞后续调度。
    - timeout 不是冷却期，而是锁的最大存活时间——仅当 worker 崩溃未能释放锁时，
      timeout 到期后锁自动过期，防止死锁。正常流程中锁的实际持有时间 = 函数执行时间。

    参数:
        timeout: 锁最大存活秒数（应大于任务最长预期执行时间）。
        use_params: 为 True 时按参数区分锁（同函数不同参数可并行）。
    """

    def task_decorator(task_func):
        @wraps(task_func)
        def wrapper(*args, **kwargs):
            if use_params:
                params_hash = _generate_func_key(task_func, *args, **kwargs)
                lock_id = f"{task_func.__name__}-locked-{params_hash}"
            else:
                lock_id = f"{task_func.__name__}-locked"

            lock_token = uuid4().hex
            acquired = cache.add(lock_id, lock_token, timeout)
            if not acquired:
                return None

            try:
                return task_func(*args, **kwargs)
            finally:
                if cache.get(lock_id) == lock_token:
                    cache.delete(lock_id)

        return wrapper

    return task_decorator


def _generate_func_key(func, *args, **kwargs):
    """
    根据函数名和参数生成唯一的哈希 key
    """

    try:
        # 使用 json 序列化确保顺序一致
        kwargs_str = json.dumps(kwargs, sort_keys=True, default=str)
    except Exception:  # noqa
        kwargs_str = str(kwargs)

    key = f"{func.__module__}.{func.__name__}:{identifying_args(args)}:{kwargs_str}"
    return sha256(key.encode("utf-8")).hexdigest()


def identifying_args(args):
    """剔除对「同一参数组合」没有标识意义、且跨进程不稳定的参数。

    bind=True 的 Celery 任务会把 Task 实例作为第一个位置参数传进来，而 Task 的 repr
    形如 `<@task: chains.tasks.confirm_transfer of xcash at 0x1042f2cf0>`——尾部是
    id(app) 的内存地址，每个 worker 进程各不相同。若让它参与 key，同一任务同一参数
    在每个进程会各自算出一把锁，分布式互斥完全失效（同 pk 的任务被多个 worker 并发执行）。
    """
    return tuple(arg for arg in args if not isinstance(arg, Task))
