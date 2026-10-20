"""升级时把 Redis 里旧版本 Celery 的在途消息迁移到 PG 任务队列：分类、幂等、零修改失败。"""

import base64
import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

import redis
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from common.jobs import PRIORITY_URGENT
from common.tests.job_helpers import queued_jobs
from core.management.commands import migrate_celery_messages as command_module

SEP = "\x06\x16"
CELERY_KEYS = ("celery", f"celery{SEP}6", "unacked", "unacked_index")
PERIODIC_QUEUE = "periodic.core.tasks.report_business_worker_health"
RATE_LIMIT_KEY = "test:ratelimit:keep-me"


def envelope(task, *, args=(), kwargs=None, task_id=None, eta=None, retries=0):
    """按 kombu Redis transport 的落盘格式构造一条 Celery 协议 2 消息信封。"""
    task_id = task_id or str(uuid4())
    body = json.dumps(
        [
            list(args),
            kwargs or {},
            {"callbacks": None, "errbacks": None, "chain": None, "chord": None},
        ]
    )
    return {
        "body": base64.b64encode(body.encode()).decode(),
        "content-encoding": "utf-8",
        "content-type": "application/json",
        "headers": {
            "lang": "py",
            "task": task,
            "id": task_id,
            "eta": eta.isoformat() if eta else None,
            "retries": retries,
        },
        "properties": {
            "correlation_id": task_id,
            "delivery_mode": 2,
            "delivery_info": {"exchange": "", "routing_key": "celery"},
            "priority": 0,
            "body_encoding": "base64",
            "delivery_tag": str(uuid4()),
        },
    }


class MigrateCeleryMessagesTests(TestCase):
    def setUp(self):
        self.redis = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        self.clear_celery_keys()
        self.redis.set(RATE_LIMIT_KEY, "1")

    def tearDown(self):
        self.clear_celery_keys()
        self.redis.delete(RATE_LIMIT_KEY)
        self.redis.close()

    def clear_celery_keys(self):
        self.redis.delete(*CELERY_KEYS, PERIODIC_QUEUE, "_kombu.binding.celery")

    def push(self, message, *, key="celery"):
        self.redis.lpush(key, json.dumps(message))

    def run_command(self, *args) -> str:
        output = StringIO()
        call_command("migrate_celery_messages", *args, stdout=output)
        return output.getvalue()

    def test_migrates_non_rebuildable_tasks_and_drops_the_rest(self):
        eta = timezone.now() + timedelta(minutes=30)
        callback = envelope(
            "common.saas_callback._deliver_saas_callback",
            kwargs={"payload": {"event": "invoice.confirmed", "sys_no": "INV-1"}},
            eta=eta,
            retries=3,
        )
        self.push(callback)
        # 被硬杀的 worker 留在 unacked 里的同一条消息：按消息 id 去重。
        self.redis.hset("unacked", "tag-1", json.dumps([callback, "", "celery"]))
        self.redis.zadd("unacked_index", {"tag-1": 1})
        self.push(
            envelope("evm.saas_gas_billing.retry_vault_slot_deploy_gas_fee", args=[7])
        )
        self.push(envelope("chains.tasks.schedule_vault_slot_deploy", args=[5]))
        self.push(envelope("aml.tasks.screen_transfer_aml", args=[9]))
        self.push(
            envelope("chains.tasks.confirm_transfer", args=[3]), key=f"celery{SEP}6"
        )
        self.push(
            envelope("core.tasks.report_business_worker_health"), key=PERIODIC_QUEUE
        )
        self.redis.sadd("_kombu.binding.celery", "celery")

        output = self.run_command()

        # 审计行：SaaS 回调只打 event / appid / sys_no 摘要；统计不含重复的那一份。
        self.assertIn("迁移 4 条，丢弃 2 条，未知 0 条", output)
        self.assertIn('"sys_no": "INV-1"', output)
        [callback_job] = queued_jobs("common.deliver_saas_callback")
        self.assertEqual(
            callback_job.args,
            {"payload": {"event": "invoice.confirmed", "sys_no": "INV-1"}},
        )
        self.assertEqual(callback_job.priority, PRIORITY_URGENT)
        self.assertEqual(
            callback_job.queueing_lock, f"celery:{callback['headers']['id']}"
        )
        # 原 eta 写入计划执行时刻；重试次数不保留。
        self.assertAlmostEqual(
            (callback_job.scheduled_at - eta).total_seconds(), 0, delta=1
        )
        self.assertEqual(callback_job.attempts, 0)
        self.assertEqual(
            [job.args for job in queued_jobs("evm.report_vault_slot_deploy_gas_fee")],
            [{"tx_task_id": 7}],
        )
        self.assertEqual(
            [job.args for job in queued_jobs("chains.schedule_vault_slot_deploy")],
            [{"slot_pk": 5}],
        )
        self.assertEqual(
            [job.args for job in queued_jobs("aml.screen_transfer_aml")],
            [{"transfer_id": 9}],
        )
        # 可由数据库重建的确认任务、周期探针直接丢弃，不进新队列。
        self.assertEqual(queued_jobs("chains.confirm_transfer"), [])
        self.assertEqual(queued_jobs("core.report_worker_health"), [])
        self.assertEqual(self.redis.exists(*CELERY_KEYS, PERIODIC_QUEUE), 0)
        self.assertFalse(self.redis.exists("_kombu.binding.celery"))
        # 同库里的限流计数不受影响。
        self.assertEqual(self.redis.get(RATE_LIMIT_KEY), "1")

    def test_past_eta_runs_immediately(self):
        self.push(
            envelope(
                "aml.tasks.screen_transfer_aml",
                args=[9],
                eta=timezone.now() - timedelta(minutes=5),
            )
        )

        self.run_command()

        [job] = queued_jobs("aml.screen_transfer_aml")
        self.assertIsNone(job.scheduled_at)

    def test_unknown_task_aborts_without_any_change(self):
        self.push(envelope("aml.tasks.screen_transfer_aml", args=[9]))
        self.push(envelope("aml.tasks.screen_invoice_aml", args=[1]))
        self.redis.lpush("celery", "not json")

        with self.assertRaisesMessage(CommandError, "aml.tasks.screen_invoice_aml"):
            self.run_command()

        self.assertEqual(self.redis.llen("celery"), 3)
        self.assertEqual(queued_jobs("aml.screen_transfer_aml"), [])

    def test_dry_run_changes_nothing(self):
        self.push(envelope("aml.tasks.screen_transfer_aml", args=[9]))

        self.run_command("--dry-run")

        self.assertEqual(self.redis.llen("celery"), 1)
        self.assertEqual(queued_jobs("aml.screen_transfer_aml"), [])

    def test_rerun_after_crash_before_redis_cleanup_does_not_duplicate(self):
        self.push(envelope("aml.tasks.screen_transfer_aml", args=[9]))

        # PG 已提交、删除 Redis 键之前进程崩溃。
        with (
            patch.object(
                redis.client.Pipeline,
                "execute",
                side_effect=redis.ConnectionError("lost"),
            ),
            self.assertRaises(redis.ConnectionError),
        ):
            self.run_command()
        self.assertEqual(self.redis.llen("celery"), 1)
        self.assertEqual(len(queued_jobs("aml.screen_transfer_aml")), 1)

        self.run_command()
        self.run_command()

        self.assertEqual(len(queued_jobs("aml.screen_transfer_aml")), 1)
        self.assertFalse(self.redis.exists("celery"))

    def test_write_between_snapshot_and_cleanup_is_retried_and_migrated(self):
        self.push(envelope("aml.tasks.screen_transfer_aml", args=[1]))
        original = command_module.Command.defer_migrated_jobs
        calls = []

        def defer_then_write(messages):
            original(messages)
            if not calls:
                # 快照之后有新消息写入：WATCH 冲突，整轮重来。
                self.push(envelope("aml.tasks.screen_transfer_aml", args=[2]))
            calls.append(len(messages))

        with patch.object(
            command_module.Command,
            "defer_migrated_jobs",
            side_effect=defer_then_write,
        ):
            self.run_command()

        self.assertEqual(calls, [1, 2])
        self.assertEqual(
            sorted(
                job.args["transfer_id"]
                for job in queued_jobs("aml.screen_transfer_aml")
            ),
            [1, 2],
        )
        self.assertFalse(self.redis.exists("celery"))

    def test_empty_redis_is_a_no_op(self):
        self.run_command()

        self.assertEqual(self.redis.get(RATE_LIMIT_KEY), "1")
