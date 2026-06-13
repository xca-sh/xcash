from __future__ import annotations

import time
from collections import defaultdict
from typing import TYPE_CHECKING

import structlog
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

if TYPE_CHECKING:
    from datetime import timedelta

from chains.models import TxTaskStatus
from core.runtime_settings import get_webhook_event_timeout
from webhooks.models import WebhookEvent

logger = structlog.get_logger()

# EVM/Tron 资源水位需要多链实时 RPC，badge 等高频展示入口不能在每次页面渲染时
# 触发。统一由异步巡检 scan_operational_risks 把计数写入缓存，展示层只读缓存。
# TTL 取调度周期（默认 120s）的数倍：连续多轮巡检缺失即回落到 0（fail-open），
# 避免 worker 长期停摆后 badge 永久卡在过期的风险态。
RESOURCE_RISK_COUNT_CACHE_KEY = "operational_risk:resource_risk_counts"
RESOURCE_RISK_COUNT_CACHE_TTL = 600
EMPTY_RESOURCE_RISK_COUNTS = {
    "evm_low_native_balance_count": 0,
    "tron_low_resource_count": 0,
}


class OperationalRiskService:
    """统一收口后台巡检阈值，避免仪表盘与异步巡检出现两套口径。"""

    @classmethod
    def webhook_event_timeout(cls) -> timedelta:
        return get_webhook_event_timeout()

    @classmethod
    def stalled_webhook_events(cls):
        now = timezone.now()
        return WebhookEvent.objects.filter(
            status=WebhookEvent.Status.PENDING,
            created_at__lte=now - cls.webhook_event_timeout(),
        ).select_related("project")

    @classmethod
    def evm_low_native_balance_alerts(cls, *, limit: int = 8) -> list[dict]:
        """按在途主动任务估算 EVM sender 需要的原生币余额。"""
        from evm.models import EvmTxTask

        grouped = defaultdict(
            lambda: {
                "chain": None,
                "sender": None,
                "required_balance": 0,
                "task_count": 0,
                "error": "",
            }
        )
        gas_price_cache: dict[int, int] = {}
        tasks = (
            EvmTxTask.objects.select_related("base_task", "chain", "sender")
            .filter(
                chain__active=True,
                base_task__status__in=[
                    TxTaskStatus.QUEUED,
                    TxTaskStatus.SUBMITTED,
                ],
            )
            .order_by("chain_id", "sender_id", "nonce")
        )
        for task in tasks:
            gas_price = task.gas_price
            if gas_price is None:
                try:
                    if task.chain_id not in gas_price_cache:
                        gas_price_cache[task.chain_id] = int(task.chain.w3.eth.gas_price)
                    gas_price = gas_price_cache[task.chain_id]
                except Exception as exc:  # noqa: BLE001
                    key = (task.chain_id, task.sender_id)
                    grouped[key]["chain"] = task.chain
                    grouped[key]["sender"] = task.sender
                    grouped[key]["task_count"] += 1
                    grouped[key]["error"] = str(exc)
                    continue

            gas_cost = int(task.gas) * int(gas_price)
            required = int(task.value) + gas_cost
            if task.base_task.status == TxTaskStatus.QUEUED:
                # 与广播前 preflight 保持同口径：未进 mempool 的任务保留 2x gas 缓冲。
                required += gas_cost

            key = (task.chain_id, task.sender_id)
            grouped[key]["chain"] = task.chain
            grouped[key]["sender"] = task.sender
            grouped[key]["required_balance"] += required
            grouped[key]["task_count"] += 1

        alerts = []
        for data in grouped.values():
            chain = data["chain"]
            sender = data["sender"]
            if data["error"]:
                alerts.append(
                    {
                        **data,
                        "current_balance": None,
                    }
                )
                if len(alerts) >= limit:
                    break
                continue
            try:
                current_balance = int(chain.w3.eth.get_balance(sender.address))
            except Exception as exc:  # noqa: BLE001
                alerts.append(
                    {
                        **data,
                        "current_balance": None,
                        "error": str(exc),
                    }
                )
                if len(alerts) >= limit:
                    break
                continue
            if current_balance < data["required_balance"]:
                alerts.append(
                    {
                        **data,
                        "current_balance": current_balance,
                        "error": "",
                    }
                )
            if len(alerts) >= limit:
                break
        return alerts

    @classmethod
    def tron_low_resource_alerts(cls, *, limit: int = 8) -> list[dict]:
        """按待广播/需重签任务估算 Tron sender 资源水位。"""
        from tron.client import TronHttpClient
        from tron.models import TronTxTask
        from tron.resources import available_bandwidth
        from tron.resources import available_energy
        from tron.resources import bandwidth_safety_bytes
        from tron.resources import estimate_contract_call_energy
        from tron.resources import estimate_signed_transaction_bandwidth
        from tron.resources import with_safety_margin

        now_ms = int(time.time() * 1000)
        grouped = defaultdict(list)
        tasks = (
            TronTxTask.objects.select_related("base_task", "chain", "sender")
            .filter(
                chain__active=True,
            )
            .filter(
                Q(base_task__status=TxTaskStatus.QUEUED)
                | Q(
                    base_task__status=TxTaskStatus.SUBMITTED,
                    expiration__lte=now_ms,
                )
            )
            .order_by("chain_id", "sender_id", "created_at")
        )
        for task in tasks:
            grouped[(task.chain_id, task.sender_id)].append(task)

        alerts = []
        for group_tasks in grouped.values():
            first_task = group_tasks[0]
            chain = first_task.chain
            sender = first_task.sender
            client = TronHttpClient(chain=chain)
            try:
                resource = client.get_account_resource(address=sender.address)
            except Exception as exc:  # noqa: BLE001
                alerts.append(
                    {
                        "chain": chain,
                        "sender": sender,
                        "available_energy": None,
                        "required_energy": None,
                        "available_bandwidth": None,
                        "required_bandwidth": None,
                        "task_count": len(group_tasks),
                        "error": str(exc),
                    }
                )
                if len(alerts) >= limit:
                    break
                continue

            required_energy = 0
            required_bandwidth = 0
            for task in group_tasks:
                try:
                    estimated_energy = estimate_contract_call_energy(
                        client=client,
                        owner_address=sender.address,
                        contract_address=task.to,
                        function_selector=task.function_selector,
                        parameter=task.parameter,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Tron 资源巡检能量估算失败",
                        chain=chain.code,
                        sender=sender.address,
                        tron_task_id=task.pk,
                        error=str(exc),
                    )
                    continue
                required_energy += with_safety_margin(estimated_energy)
                if task.signed_payload:
                    required_bandwidth += (
                        estimate_signed_transaction_bandwidth(task.signed_payload)
                        + bandwidth_safety_bytes()
                    )
                else:
                    required_bandwidth += bandwidth_safety_bytes()

            current_energy = available_energy(resource)
            current_bandwidth = available_bandwidth(resource)
            if current_energy < required_energy or current_bandwidth < required_bandwidth:
                alerts.append(
                    {
                        "chain": chain,
                        "sender": sender,
                        "available_energy": current_energy,
                        "required_energy": required_energy,
                        "available_bandwidth": current_bandwidth,
                        "required_bandwidth": required_bandwidth,
                        "task_count": len(group_tasks),
                        "error": "",
                    }
                )
            if len(alerts) >= limit:
                break
        return alerts

    @classmethod
    def build_summary(cls, *, limit: int = 4, include_resource_checks: bool = False) -> dict:
        """返回后台展示与异步巡检共享的异常概览。"""
        stalled_webhook_events = cls.stalled_webhook_events()
        evm_low_native_balance_alerts = []
        tron_low_resource_alerts = []
        if include_resource_checks:
            evm_low_native_balance_alerts = cls.evm_low_native_balance_alerts(limit=limit)
            tron_low_resource_alerts = cls.tron_low_resource_alerts(limit=limit)

        return {
            "stalled_webhook_event_count": stalled_webhook_events.count(),
            "recent_stalled_webhook_events": list(
                stalled_webhook_events.order_by("created_at")[:limit]
            ),
            "evm_low_native_balance_count": len(evm_low_native_balance_alerts),
            "recent_evm_low_native_balance_alerts": evm_low_native_balance_alerts,
            "tron_low_resource_count": len(tron_low_resource_alerts),
            "recent_tron_low_resource_alerts": tron_low_resource_alerts,
        }

    @classmethod
    def cache_resource_risk_counts(
        cls,
        *,
        evm_low_native_balance_count: int,
        tron_low_resource_count: int,
    ) -> None:
        """把异步巡检算出的资源水位计数写入缓存，供 badge 等展示入口低成本读取。

        由 scan_operational_risks 每轮无条件调用（含清零），保证展示层读到的是
        最近一次巡检结果，而非渲染时实时打多链 RPC。
        """
        cache.set(
            RESOURCE_RISK_COUNT_CACHE_KEY,
            {
                "evm_low_native_balance_count": int(evm_low_native_balance_count),
                "tron_low_resource_count": int(tron_low_resource_count),
            },
            RESOURCE_RISK_COUNT_CACHE_TTL,
        )

    @classmethod
    def cached_resource_risk_counts(cls) -> dict:
        """读取最近一次巡检缓存的资源水位计数；缓存缺失或损坏时回落到 0。"""
        cached = cache.get(RESOURCE_RISK_COUNT_CACHE_KEY)
        if not isinstance(cached, dict):
            return dict(EMPTY_RESOURCE_RISK_COUNTS)
        return {
            "evm_low_native_balance_count": int(
                cached.get("evm_low_native_balance_count") or 0
            ),
            "tron_low_resource_count": int(
                cached.get("tron_low_resource_count") or 0
            ),
        }
