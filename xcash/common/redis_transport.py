"""Redis 队列原子合并：保留已有 tick，空队列才写入；无锁、无租约、无额外 key。"""

import json

from kombu.compression import decompress
from kombu.transport.redis import Channel as RedisChannel
from kombu.transport.redis import Transport as RedisTransport
from kombu.utils.json import dumps

from config.periodic_tasks import PERIODIC_QUEUE_TASKS
from config.periodic_tasks import PERIODIC_TASK_QUEUES

ENQUEUE_ONCE_SCRIPT = """
for _, key in ipairs(KEYS) do
    if redis.call("LLEN", key) > 0 then
        return 0
    end
end
return redis.call("LPUSH", KEYS[1], ARGV[1])
"""


class Channel(RedisChannel):
    def basic_publish(self, message, exchange, routing_key, **kwargs):
        """在序列化边界约束可合并消息，普通任务发布和 send_task 均受校验。"""
        headers = message.get("headers", {})
        task = headers.get("task")
        queue = PERIODIC_TASK_QUEUES.get(task)
        if queue is not None:
            # 只允许默认 direct exchange，routing_key 就是唯一目标队列。
            # 禁止路由覆盖和 fanout，以免部分副本绕开原子合并。
            if exchange or routing_key != queue:
                raise ValueError(f"周期任务 {task} 必须投递到专属队列 {queue}")
            if message["content-type"] != "application/json":
                raise ValueError("可合并周期任务仅支持 JSON 序列化")
            body = message["body"]
            if headers.get("compression"):
                body = decompress(body, headers["compression"])
            args, task_kwargs, embedded = json.loads(body)
            if args or task_kwargs:
                raise TypeError("可合并周期任务不允许携带参数")
            if not headers.get("ignore_result"):
                raise ValueError("可合并周期任务必须忽略独立执行结果")
            if (
                headers.get("eta")
                or headers.get("expires")
                or headers.get("group")
                or message["properties"].get("expiration")
                or any((embedded or {}).values())
            ):
                raise ValueError("可合并周期任务不支持延时、过期或 Canvas 回调")
        elif routing_key in PERIODIC_QUEUE_TASKS:
            raise ValueError("周期专属队列不能接收其他任务")
        return super().basic_publish(message, exchange, routing_key, **kwargs)

    def enqueue_once(self, client, queue, message):
        """普通发布和 unacked 重投共用，client 也可以是 Kombu 的事务 pipeline。"""
        priority = self._get_message_priority(message, reverse=False)
        # 检查所有优先级分桶，不能每个分桶各留一条。第一个 key 是本次实际写入点。
        keys = list(
            dict.fromkeys(
                self.global_keyprefix + self._q_for_pri(queue, step)
                for step in (priority, *self.priority_steps)
            )
        )
        # Kombu 的 global_keyprefix 包装器不处理 EVAL，需在此显式加前缀。
        # Redis 失败直接向上抛出，绝不能回退为普通 LPUSH。
        return client.eval(ENQUEUE_ONCE_SCRIPT, len(keys), *keys, dumps(message))

    def put(self, queue, message, **kwargs):
        if queue not in PERIODIC_QUEUE_TASKS:
            return super()._put(queue, message, **kwargs)
        with self.conn_or_acquire() as client:
            return self.enqueue_once(client, queue, message)

    def restore_message(
        self,
        payload,
        exchange,
        routing_key,
        pipe,
        leftmost=False,  # noqa: FBT002 - Kombu 通过位置参数调用此扩展点
    ):
        if not exchange and routing_key in PERIODIC_QUEUE_TASKS:
            payload.setdefault("headers", {})["redelivered"] = True
            payload["properties"]["delivery_info"]["redelivered"] = True
            # EVAL 与 Kombu 的 unacked 删除处于同一事务。若已有更新的 tick，
            # 直接合并旧副本；队列为空才恢复，不依赖恢复顺序或 worker 回调。
            self.enqueue_once(pipe, routing_key, payload)
        else:
            # 普通业务消息以及升级前旧队列中的消息保留 Kombu 原有重投语义。
            super()._do_restore_message(payload, exchange, routing_key, pipe, leftmost)

    # Kombu 扩展点的名称由上游规定，项目内实现方法仍使用公开名称。
    _put = put
    _do_restore_message = restore_message


class Transport(RedisTransport):
    Channel = Channel
