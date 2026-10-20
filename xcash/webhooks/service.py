from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from django.conf import settings

from webhooks.models import WebhookEvent


def delivery_url_scheme_allowed(url: str) -> bool:
    """回调地址能否被投递，供各录入入口提前拒绝。

    投递只接受 https（见 webhooks.tasks.safe_delivery_target）：入口若放行 http，
    要等到付款后真正投递时才判为失败，商户收不到支付通知。内网放行模式（本地联调、
    压测）下投递也接受 http，入口保持同一口径。
    """
    if getattr(settings, "WEBHOOK_ALLOW_INTERNAL_TARGETS", False):
        return True
    return urlsplit(url).scheme == "https"


class WebhookService:
    """封装 webhook 事件的创建与状态更新，供业务模块调用。"""

    @staticmethod
    def enqueue_delivery(event: WebhookEvent) -> None:
        """在事件创建的事务内入队投递任务：同事务提交，回滚时事件与任务一起消失。"""
        from webhooks.tasks import enqueue_deliver_event

        enqueue_deliver_event(event.pk)

    @staticmethod
    def create_event(
        *,
        project,
        payload: dict[str, Any],
        delivery_url: str = "",
        delivery_method: str = WebhookEvent.DeliveryMethod.POST_JSON,
        expected_response_body: str = "ok",
    ) -> WebhookEvent:
        event = WebhookEvent.objects.create(
            project=project,
            payload=payload,
            delivery_url=delivery_url,
            delivery_method=delivery_method,
            expected_response_body=expected_response_body,
        )
        WebhookService.enqueue_delivery(event)
        return event
