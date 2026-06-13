from __future__ import annotations

import time
from collections import defaultdict
from typing import TYPE_CHECKING

import structlog
from django.db.models import Q
from django.utils import timezone

if TYPE_CHECKING:
    from datetime import timedelta

from chains.models import TxTaskStatus
from core.runtime_settings import get_webhook_event_timeout
from webhooks.models import WebhookEvent

logger = structlog.get_logger()


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
