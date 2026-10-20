"""一次性迁移：把旧版本 Celery 留在 Redis 里的在途消息转进 PostgreSQL 任务队列。

由 scripts/upgrade.sh 在数据库迁移之后、启动新进程之前调用（此时旧进程已全部停止，
Redis 不再有新消息写入）。Redis 空时是空操作，开源部署跨版本升级时同样适用。

规则（确定、幂等、可审计）：
- MIGRATE：不能由数据库重建的任务（SaaS 回调、gas 计费、VaultSlot 预部署、AML 查询），
  转成对应的新任务；原 eta 未到期的写入计划执行时刻，已过期的立即执行；原重试次数不保留。
- DROP：可由数据库重建（转账处理与确认、Webhook 投递由兜底周期任务重新派发）或已无意义
  （权限刷新、全部周期任务、历次版本删除的旧任务）的，直接丢弃。
- UNKNOWN：其余任务名（含无法解析的消息）一律打印清单、零修改、非零退出，交运维判断。

步骤：WATCH 全部相关键后读快照 → 解析并按消息 id 去重（队列与 unacked 里可能各有一份）→
逐条打印审计行 → 一个 PG 事务内入队全部 MIGRATE（queueing_lock 取 "celery:<消息 id>"）→
MULTI/EXEC 删除这些键。EXEC 因 WATCH 失败（期间有写入）则整轮重来，已入队的会因
queueing_lock 冲突被跳过；PG 提交后、删除前崩溃，重跑同样全部跳过再删除。

消息格式不依赖 kombu（新版本已不安装）：kombu Redis transport 把消息信封 JSON 存在 LIST
里，优先级分桶的键名为「队列名 + \\x06\\x16 + 3/6/9」；unacked 存在 HASH，值为
[信封, exchange, routing_key]。信封的 body 是 base64 编码的 Celery 协议 2
[args, kwargs, embed]，headers 含 task、id、eta、retries。

只删除上面这些 Celery 专用的键，不做 FLUSHDB：同一逻辑库里还有限流计数。
"""

from __future__ import annotations

import base64
import dataclasses
import json
import zlib
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any

import redis
from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone

from common.jobs import PRIORITY_URGENT
from common.jobs import defer_once

PRIORITY_SEP = "\x06\x16"
UNACKED_KEY = "unacked"
UNACKED_EXTRA_KEYS = ("unacked_index", "unacked_mutex")
# 任务队列：业务队列 celery、阶段 1 之前的扫描队列 scan、周期专属队列 periodic.*；
# 各自带优先级分桶。
TASK_QUEUE_PATTERNS = (
    "celery",
    f"celery{PRIORITY_SEP}*",
    "scan",
    f"scan{PRIORITY_SEP}*",
    "periodic.*",
)
# 只删不读的 Celery 控制面残留：交换机绑定与 inspect 的回复队列。
CONTROL_KEY_PATTERNS = ("_kombu.binding.*", "*.reply.celery.pidbox")
REDIS_SOCKET_TIMEOUT_SECONDS = 5
MAX_WATCH_RETRIES = 3


@dataclass(frozen=True)
class MigrateRule:
    """旧任务如何转成新任务：新任务的导入路径、唯一入参的参数名、优先级。"""

    task_path: str
    argument: str
    priority: int | None = None


# gas 计费、预部署与 AML 的旧任务只有一个位置参数；SaaS 回调是关键字参数 payload。
MIGRATE_TASKS = {
    "common.saas_callback._deliver_saas_callback": MigrateRule(
        "common.saas_callback.deliver_saas_callback",
        "payload",
        priority=PRIORITY_URGENT,
    ),
    "evm.saas_gas_billing.retry_vault_slot_deploy_gas_fee": MigrateRule(
        "evm.saas_gas_billing.report_vault_slot_deploy_gas_fee", "tx_task_id"
    ),
    "evm.saas_gas_billing.retry_vault_slot_collect_gas_fee": MigrateRule(
        "evm.saas_gas_billing.report_vault_slot_collect_gas_fee", "tx_task_id"
    ),
    "tron.saas_gas_billing.retry_vault_slot_deploy_gas_fee": MigrateRule(
        "tron.saas_gas_billing.report_vault_slot_deploy_gas_fee", "tx_task_id"
    ),
    "tron.saas_gas_billing.retry_vault_slot_collect_gas_fee": MigrateRule(
        "tron.saas_gas_billing.report_vault_slot_collect_gas_fee", "tx_task_id"
    ),
    "chains.tasks.schedule_vault_slot_deploy": MigrateRule(
        "chains.tasks.schedule_vault_slot_deploy", "slot_pk"
    ),
    "aml.tasks.screen_transfer_aml": MigrateRule(
        "aml.tasks.screen_transfer_aml", "transfer_id"
    ),
}

DROP_TASKS = frozenset(
    {
        # 可由数据库重建：兜底周期任务与链高推进会重新派发。
        "chains.tasks.process_transfer",
        "chains.tasks.confirm_transfer",
        "chains.tasks.block_number_updated",
        "webhooks.tasks.deliver_event",
        "invoices.tasks.check_expired",
        # 缓存维护，下一次请求会重新触发。
        "common.permission_check._refresh_saas_permission",
        # 周期任务：新版本由 worker 按 cron 重新投递。
        "webhooks.tasks.schedule_events",
        "webhooks.tasks.reap_stalled_events",
        "chains.tasks.fallback_process_transfer",
        "chains.tasks.execute_due_vault_slot_collect_schedules",
        "chains.tasks.reconcile_vault_slot_collect_balance_gaps_task",
        "chains.tasks.reap_stale_confirming_transfers",
        "evm.tasks.scan_stuck_queued_evm_tx_tasks",
        "currencies.tasks.refresh_crypto_prices",
        "invoices.tasks.fallback_invoice_expired",
        "core.tasks.scan_operational_risks",
        "core.tasks.report_business_worker_health",
        "celery.backend_cleanup",
        # 历次版本删除的任务：扫描与主动交易已移入 chain-runner，压测 app 已删除。
        "evm.tasks._broadcast_evm_task",
        "evm.tasks.dispatch_evm_tx_tasks",
        "evm.tasks.poll_active_evm_chains",
        "evm.tasks.poll_evm_chain_tx_tasks",
        "evm.tasks.scan_active_evm_chains",
        "evm.tasks._scan_evm_chain",
        "tron.tasks.broadcast_tron_task",
        "tron.tasks.dispatch_tron_tx_tasks",
        "tron.tasks.confirm_tron_receipt_tx_tasks",
        "tron.tasks.scan_active_tron_chains",
        "tron.tasks.scan_tron_chain",
        "core.tasks.report_scan_worker_health",
        "stress.tasks.check_deposit_webhook_timeout",
        "stress.tasks.check_webhook_timeout",
        "stress.tasks.execute_deposit_case",
        "stress.tasks.execute_deposit_case_payment",
        "stress.tasks.execute_stress_case",
        "stress.tasks.execute_stress_case_payment",
        "stress.tasks.finalize_stress_timeout",
        "stress.tasks.prepare_stress",
        "stress.tasks.verify_deposit_collection",
        "stress.tasks.verify_invoice_collection",
    }
)


class MessageFormatError(Exception):
    """消息信封无法按 Celery 协议 2 解析。"""


@dataclass(frozen=True)
class CeleryMessage:
    message_id: str
    task: str
    args: list
    kwargs: dict
    eta: datetime | None
    retries: int
    source: str


@dataclass(frozen=True)
class Snapshot:
    messages: list[CeleryMessage]
    malformed: list[tuple[str, str]]
    keys_to_delete: list[str]


def decode_body(envelope: dict) -> bytes:
    """取出信封里的 body 原文（base64 解码、解压）；不支持的格式抛 MessageFormatError。"""
    if envelope.get("content-type") != "application/json":
        raise MessageFormatError(f"不支持的序列化 {envelope.get('content-type')}")
    body = envelope.get("body")
    if not isinstance(body, str):
        raise MessageFormatError("缺少 body")
    compression = envelope["headers"].get("compression")
    if compression and compression not in ("application/x-gzip", "gzip", "zlib"):
        raise MessageFormatError(f"不支持的压缩 {compression}")
    try:
        raw = (
            base64.b64decode(body)
            if envelope.get("properties", {}).get("body_encoding") == "base64"
            else body.encode()
        )
        return zlib.decompress(raw) if compression else raw
    except (ValueError, zlib.error) as exc:
        raise MessageFormatError(f"body 解码失败: {exc}") from exc


def parse_envelope(envelope: Any, *, source: str) -> CeleryMessage:
    """把 kombu 消息信封解析成 CeleryMessage；格式不符抛 MessageFormatError。"""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("headers"), dict):
        raise MessageFormatError("不是 kombu 消息信封")
    body = decode_body(envelope)
    headers = envelope["headers"]
    try:
        args, kwargs, _embed = json.loads(body)
        eta = datetime.fromisoformat(headers["eta"]) if headers.get("eta") else None
        message = CeleryMessage(
            message_id=str(headers["id"]),
            task=str(headers["task"]),
            args=list(args or []),
            kwargs=dict(kwargs or {}),
            eta=eta,
            retries=int(headers.get("retries") or 0),
            source=source,
        )
    except Exception as exc:
        raise MessageFormatError(f"{type(exc).__name__}: {exc}") from exc
    if eta is not None and eta.tzinfo is None:
        # Celery 默认写带时区的 UTC 时刻；兼容未带时区的写法，按 UTC 理解。
        message = dataclasses.replace(message, eta=eta.replace(tzinfo=UTC))
    return message


def task_argument(message: CeleryMessage, name: str) -> Any:
    """取旧任务唯一的入参：调用方可能按位置或按关键字传入。"""
    if message.args:
        return message.args[0]
    if name in message.kwargs:
        return message.kwargs[name]
    raise MessageFormatError(f"缺少参数 {name}")


def argument_summary(message: CeleryMessage) -> str:
    """审计行里的参数摘要：SaaS 回调只打 event / appid / sys_no，不打完整 payload。"""
    if message.task == "common.saas_callback._deliver_saas_callback":
        payload = message.kwargs.get("payload") or {}
        return json.dumps(
            {key: payload.get(key) for key in ("event", "appid", "sys_no")},
            ensure_ascii=False,
        )
    if message.task in MIGRATE_TASKS:
        name = MIGRATE_TASKS[message.task].argument
        try:
            return f"{name}={task_argument(message, name)}"
        except MessageFormatError:
            return "-"
    return "-"


def resolve_task(dotted_path: str):
    from django.utils.module_loading import import_string  # noqa: PLC0415

    return import_string(dotted_path)


class Command(BaseCommand):
    help = "把 Redis 里旧版本 Celery 的在途消息迁移到 PostgreSQL 任务队列（升级时一次性执行）"
    # upgrade.sh 在生产迁移之前就用 --dry-run 预览，此时库结构还是旧版本，不跑系统检查。
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只打印分类与审计结果，不修改 Redis 与数据库",
        )

    def handle(self, *args, **options):
        client = redis.Redis.from_url(
            settings.REDIS_URL,
            socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
            decode_responses=True,
        )
        try:
            for _attempt in range(MAX_WATCH_RETRIES):
                if self.migrate_once(client, dry_run=options["dry_run"]):
                    return
                self.stdout.write("快照读取后 Redis 有新写入，重新读取并迁移")
            raise CommandError(
                f"Redis 在迁移期间持续有新写入，{MAX_WATCH_RETRIES} 次重试均未完成；"
                "请确认旧的 worker / beat / django 已全部停止后重跑"
            )
        finally:
            client.close()

    def migrate_once(self, client: redis.Redis, *, dry_run: bool) -> bool:
        """执行一轮；返回 False 表示 EXEC 因 WATCH 冲突失败、需要重来。"""
        task_keys = self.matching_keys(client, TASK_QUEUE_PATTERNS, key_type="list")
        control_keys = self.matching_keys(client, CONTROL_KEY_PATTERNS)
        with client.pipeline() as pipe:
            pipe.watch(*task_keys, UNACKED_KEY, *UNACKED_EXTRA_KEYS)
            snapshot = self.read_snapshot(pipe, task_keys)
            self.report(snapshot)
            if dry_run:
                self.stdout.write("--dry-run：未修改 Redis 与数据库")
                return True
            self.defer_migrated_jobs(snapshot.messages)
            keys = [*snapshot.keys_to_delete, *UNACKED_EXTRA_KEYS, *control_keys]
            pipe.multi()
            pipe.delete(*keys)
            try:
                (deleted,) = pipe.execute()
            except redis.WatchError:
                return False
        self.stdout.write(f"已删除 {deleted} 个 Celery 相关的 Redis 键")
        return True

    @staticmethod
    def matching_keys(
        client: redis.Redis,
        patterns: tuple[str, ...],
        *,
        key_type: str | None = None,
    ) -> list[str]:
        keys: set[str] = set()
        for pattern in patterns:
            for key in client.scan_iter(match=pattern, count=500):
                if key_type is None or client.type(key) == key_type:
                    keys.add(key)
        return sorted(keys)

    @staticmethod
    def read_snapshot(pipe, task_keys: list[str]) -> Snapshot:
        """在 WATCH 之后读取全部消息（pipeline 此时处于立即执行模式）。"""
        raw: list[tuple[str, str]] = []
        for key in task_keys:
            raw.extend((key, item) for item in pipe.lrange(key, 0, -1))
        unacked = pipe.hgetall(UNACKED_KEY)
        messages: list[CeleryMessage] = []
        malformed: list[tuple[str, str]] = []
        seen: set[str] = set()

        def collect(source: str, envelope: Any) -> None:
            try:
                message = parse_envelope(envelope, source=source)
            except MessageFormatError as exc:
                malformed.append((source, str(exc)))
                return
            if message.message_id in seen:
                return
            seen.add(message.message_id)
            messages.append(message)

        for key, item in raw:
            try:
                envelope = json.loads(item)
            except ValueError as exc:
                malformed.append((key, f"JSON 解析失败: {exc}"))
                continue
            collect(key.replace(PRIORITY_SEP, "#"), envelope)
        for tag, value in unacked.items():
            try:
                envelope, _exchange, _routing_key = json.loads(value)
            except ValueError as exc:
                malformed.append((f"unacked:{tag}", f"JSON 解析失败: {exc}"))
                continue
            collect(f"unacked:{tag}", envelope)
        keys_to_delete = [*task_keys, UNACKED_KEY] if (task_keys or unacked) else []
        return Snapshot(
            messages=messages,
            malformed=malformed,
            keys_to_delete=keys_to_delete,
        )

    def report(self, snapshot: Snapshot) -> None:
        """逐条打印审计行与分类统计；存在未知任务或无法解析的消息时零修改退出。"""
        counts = {"MIGRATE": 0, "DROP": 0, "UNKNOWN": 0}
        unknown: list[str] = []
        for message in snapshot.messages:
            if message.task in MIGRATE_TASKS:
                action = "MIGRATE"
                try:
                    task_argument(message, MIGRATE_TASKS[message.task].argument)
                except MessageFormatError:
                    action = "UNKNOWN"
            elif message.task in DROP_TASKS:
                action = "DROP"
            else:
                action = "UNKNOWN"
            counts[action] += 1
            if action == "UNKNOWN":
                unknown.append(f"{message.task}（id={message.message_id}）")
            self.stdout.write(
                f"[{action}] task={message.task} id={message.message_id} "
                f"eta={message.eta.isoformat() if message.eta else '-'} "
                f"retries={message.retries} source={message.source} "
                f"args={argument_summary(message)}"
            )
        for source, error in snapshot.malformed:
            counts["UNKNOWN"] += 1
            unknown.append(f"无法解析的消息（{source}：{error}）")
            self.stdout.write(f"[UNKNOWN] source={source} error={error}")
        self.stdout.write(
            "Celery 在途消息：迁移 {MIGRATE} 条，丢弃 {DROP} 条，未知 {UNKNOWN} 条".format(
                **counts
            )
        )
        if unknown:
            raise CommandError(
                "存在未知任务或无法解析的消息，未做任何修改：" + "；".join(unknown)
            )

    @staticmethod
    def defer_migrated_jobs(messages: list[CeleryMessage]) -> None:
        """一个 PG 事务内入队全部 MIGRATE 消息；同一消息重复迁移会因 queueing_lock 被跳过。"""
        now = timezone.now()
        with transaction.atomic():
            for message in messages:
                if message.task not in MIGRATE_TASKS:
                    continue
                rule = MIGRATE_TASKS[message.task]
                schedule_at = message.eta if message.eta and message.eta > now else None
                defer_once(
                    resolve_task(rule.task_path),
                    queueing_lock=f"celery:{message.message_id}",
                    priority=rule.priority,
                    schedule_at=schedule_at,
                    **{rule.argument: task_argument(message, rule.argument)},
                )
