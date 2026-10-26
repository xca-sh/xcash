from rest_framework.mixins import ListModelMixin
from rest_framework.mixins import RetrieveModelMixin
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import GenericViewSet
from saas_api.authentication import SaasTokenAuthentication
from saas_api.list_filters import filter_created_range
from saas_api.list_filters import keyword_param
from saas_api.serializers.webhooks import DeliveryAttemptSerializer
from saas_api.serializers.webhooks import WebhookEventSerializer

from common.error_codes import ErrorCode
from common.exceptions import APIError
from webhooks.models import DeliveryAttempt
from webhooks.models import WebhookEvent

# 商户 webhook 的事件类型写在 payload.type，目前只有账单与用户充值两种
WEBHOOK_EVENT_TYPES = ("invoice", "deposit")


class WebhookEventViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    authentication_classes = [SaasTokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = WebhookEventSerializer

    def get_queryset(self):
        queryset = WebhookEvent.objects.filter(
            project__appid=self.kwargs["project_appid"]
        ).order_by("-created_at", "-pk")
        # 仅列表接受筛选；详情按 pk 取，不受筛选参数影响
        if getattr(self, "action", None) == "list":
            queryset = self.filter_list(queryset)
        return queryset

    def filter_list(self, queryset):
        """商户中心推送列表筛选：Nonce 前缀、事件类型、投递状态、创建时间区间，可任意组合。"""
        params = self.request.query_params
        # nonce 是区分大小写的 ShortUUID，按前缀匹配，粘贴完整 nonce 即精确命中
        keyword = keyword_param(params)
        if keyword:
            queryset = queryset.filter(nonce__startswith=keyword)
        event_type = params.get("type", "").strip()
        if event_type:
            if event_type not in WEBHOOK_EVENT_TYPES:
                raise APIError(
                    ErrorCode.PARAMETER_ERROR,
                    f"type 只能是 {' / '.join(WEBHOOK_EVENT_TYPES)}",
                )
            queryset = queryset.filter(payload__type=event_type)
        status = params.get("status", "").strip()
        if status:
            if status not in WebhookEvent.Status.values:
                raise APIError(
                    ErrorCode.PARAMETER_ERROR,
                    f"status 只能是 {' / '.join(WebhookEvent.Status.values)}",
                )
            queryset = queryset.filter(status=status)
        return filter_created_range(queryset, params)


class DeliveryAttemptViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    authentication_classes = [SaasTokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = DeliveryAttemptSerializer

    def get_queryset(self):
        queryset = (
            DeliveryAttempt.objects.filter(
                event__project__appid=self.kwargs["project_appid"]
            )
            .select_related("event")
            .order_by("-created_at")
        )
        # SaaS 在事件详情里按事件展示投递记录；非法 id 直接返回空列表而非报错
        event_id = self.request.query_params.get("event")
        if event_id is not None:
            if not event_id.isdigit():
                return queryset.none()
            queryset = queryset.filter(event_id=int(event_id))
        return queryset
