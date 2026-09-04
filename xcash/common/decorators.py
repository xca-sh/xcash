import json
from functools import wraps
from hashlib import sha256
from typing import override
from uuid import uuid4

import structlog
from celery import Task
from celery.contrib.django.task import DjangoTask
from celery.utils import uuid as celery_uuid
from django.core.cache import cache
from django_redis import get_redis_connection

logger = structlog.get_logger()

PENDING_ONCE_STATE_KEY = "xcash:celery:pending-once:v1"
PENDING_ONCE_PUBLISHING_LEASE_MS = 30 * 1000
PENDING_ONCE_PENDING_LEASE_MS = 10 * 60 * 1000

# pending 标记不能使用 Redis TTL：生产 Redis 采用 volatile-lru，内存吃紧时 TTL key
# 会被优先淘汰，去重保护会在最需要它时失效。租约时间写在 value 中，由 Lua 依据
# Redis 服务器时间原子判断是否允许接管。
PENDING_ONCE_ACQUIRE_SCRIPT = """
local marker = redis.call("HGET", KEYS[1], ARGV[1])
local redis_time = redis.call("TIME")
local now_ms = redis_time[1] * 1000 + math.floor(redis_time[2] / 1000)

local function store_publishing_marker()
    redis.call("HSET", KEYS[1], ARGV[1], cjson.encode({
        state = "publishing",
        task_id = ARGV[2],
        updated_at_ms = now_ms
    }))
    return {1, ARGV[2]}
end

if not marker then
    return store_publishing_marker()
end

local decoded_ok, decoded = pcall(cjson.decode, marker)
if not decoded_ok
    or type(decoded) ~= "table"
    or type(decoded.state) ~= "string"
    or type(decoded.task_id) ~= "string"
    or type(decoded.updated_at_ms) ~= "number" then
    return {-1, ""}
end

local lease_ms
if decoded.state == "publishing" then
    lease_ms = tonumber(ARGV[3])
elseif decoded.state == "pending" then
    lease_ms = tonumber(ARGV[4])
else
    return {-1, ""}
end

local age_ms = now_ms - decoded.updated_at_ms
if age_ms >= lease_ms then
    return store_publishing_marker()
end

return {0, decoded.task_id}
"""

PENDING_ONCE_PROMOTE_SCRIPT = """
local marker = redis.call("HGET", KEYS[1], ARGV[1])
if not marker then
    return 0
end

local decoded_ok, decoded = pcall(cjson.decode, marker)
if not decoded_ok or type(decoded) ~= "table" then
    return -1
end
if decoded.task_id ~= ARGV[2] then
    return 0
end

local redis_time = redis.call("TIME")
local now_ms = redis_time[1] * 1000 + math.floor(redis_time[2] / 1000)
redis.call("HSET", KEYS[1], ARGV[1], cjson.encode({
    state = "pending",
    task_id = ARGV[2],
    updated_at_ms = now_ms
}))
return 1
"""

PENDING_ONCE_RELEASE_SCRIPT = """
local marker = redis.call("HGET", KEYS[1], ARGV[1])
if not marker then
    return 0
end

local decoded_ok, decoded = pcall(cjson.decode, marker)
if not decoded_ok or type(decoded) ~= "table" then
    return -1
end
if decoded.task_id ~= ARGV[2] then
    return 0
end

return redis.call("HDEL", KEYS[1], ARGV[1])
"""


class PendingOnceTask(DjangoTask):
    """把无参数周期任务限制为常态最多一个 pending 消息。

    该基类只允许用于无参数、执行时重新查询当前状态的 Beat 任务，不能用于带参数
    的 fan-out 子任务，也不能用于单笔 Transfer、Webhook、广播等每条消息都承载
    独立业务事实的任务。

    producer 发布前先写 pending 标记，worker 真正收到消息时再释放；因此 worker
    停机时 Beat 不会按调度周期持续向 broker 写入相同 tick，而是每个逻辑租约周期
    最多补发一个。正常执行时允许一个运行任务与一个等待任务并存。现有
    singleton_task 继续负责执行期互斥，两层职责不混合。
    """

    abstract = True
    pending_once_state_key = PENDING_ONCE_STATE_KEY
    pending_once_publishing_lease_ms = PENDING_ONCE_PUBLISHING_LEASE_MS
    pending_once_pending_lease_ms = PENDING_ONCE_PENDING_LEASE_MS

    def pending_once_field(self, args, kwargs) -> str:
        payload = json.dumps(
            {
                "args": identifying_args(args or ()),
                "kwargs": kwargs or {},
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = sha256(payload.encode("utf-8")).hexdigest()
        return f"{self.name}:{digest}"

    @staticmethod
    def pending_once_text(value) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    def acquire_pending_once(self, *, field: str, task_id: str):
        client = get_redis_connection("default")
        result = client.eval(
            PENDING_ONCE_ACQUIRE_SCRIPT,
            1,
            self.pending_once_state_key,
            field,
            task_id,
            self.pending_once_publishing_lease_ms,
            self.pending_once_pending_lease_ms,
        )
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise RuntimeError("pending-once acquire 返回了非法结果")

        status = int(result[0])
        owner_task_id = self.pending_once_text(result[1])
        if status < 0:
            raise RuntimeError("pending-once 标记格式损坏")
        return client, status == 1, owner_task_id

    def promote_pending_once(self, *, client, field: str, task_id: str) -> None:
        result = int(
            client.eval(
                PENDING_ONCE_PROMOTE_SCRIPT,
                1,
                self.pending_once_state_key,
                field,
                task_id,
            )
        )
        if result < 0:
            raise RuntimeError("pending-once 标记格式损坏")

    def release_pending_once(self, *, field: str, task_id: str, phase: str) -> None:
        try:
            client = get_redis_connection("default")
            result = int(
                client.eval(
                    PENDING_ONCE_RELEASE_SCRIPT,
                    1,
                    self.pending_once_state_key,
                    field,
                    task_id,
                )
            )
            if result < 0:
                raise RuntimeError("pending-once 标记格式损坏")  # noqa: TRY301
        except Exception:  # noqa: BLE001
            # 消息已经进入 broker 或已经开始执行，此时不能把 Redis 清理异常升级为
            # 业务任务失败。无 TTL 标记会在下一次发布时按逻辑租约被安全接管。
            logger.critical(
                "Celery pending-once 标记释放失败",
                task=self.name,
                task_id=task_id,
                phase=phase,
                exc_info=True,
            )

    @override
    def apply_async(
        self,
        args=None,
        kwargs=None,
        task_id=None,
        producer=None,
        link=None,
        link_error=None,
        shadow=None,
        **options,
    ):
        if args or kwargs:
            raise TypeError("PendingOnceTask 仅允许用于无参数周期任务")

        args = tuple(args or ())
        kwargs = dict(kwargs or {})
        task_id = str(task_id or celery_uuid())
        field = self.pending_once_field(args, kwargs)

        try:
            client, acquired, owner_task_id = self.acquire_pending_once(
                field=field,
                task_id=task_id,
            )
        except Exception:  # noqa: BLE001
            # 获取门闩失败时必须 fail closed。继续 publish 会在 Redis 故障或内存压力
            # 下重新打开无界入队路径，正是本基类要消除的事故根因。
            logger.critical(
                "Celery pending-once 标记获取失败，停止本轮投递",
                task=self.name,
                task_id=task_id,
                exc_info=True,
            )
            raise

        if not acquired:
            return self.AsyncResult(owner_task_id)

        try:
            result = super().apply_async(
                args=args,
                kwargs=kwargs,
                task_id=task_id,
                producer=producer,
                link=link,
                link_error=link_error,
                shadow=shadow,
                **options,
            )
        except BaseException:
            self.release_pending_once(
                field=field,
                task_id=task_id,
                phase="publish_failed",
            )
            raise

        try:
            self.promote_pending_once(
                client=client,
                field=field,
                task_id=task_id,
            )
        except Exception:  # noqa: BLE001
            # broker publish 已成功，不能再向调用方抛错诱发重发。publishing 的短逻辑
            # 租约会在 producer 随后崩溃或 Redis 瞬断时自动恢复。
            logger.critical(
                "Celery pending-once 标记转入 pending 失败",
                task=self.name,
                task_id=task_id,
                exc_info=True,
            )
        return result

    @override
    def before_start(self, task_id, args, kwargs) -> None:
        super().before_start(task_id, args, kwargs)
        self.release_pending_once(
            field=self.pending_once_field(args, kwargs),
            task_id=task_id,
            phase="before_start",
        )

    @override
    def after_return(self, status, retval, task_id, args, kwargs, einfo) -> None:
        # 正常路径已在 before_start 释放。这里处理 before_start Redis 瞬断等异常，
        # token 比对保证旧任务结束时不会删除后续新任务的 pending 标记。
        self.release_pending_once(
            field=self.pending_once_field(args, kwargs),
            task_id=task_id,
            phase="after_return",
        )
        super().after_return(status, retval, task_id, args, kwargs, einfo)


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
