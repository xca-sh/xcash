from rest_framework.mixins import ListModelMixin
from rest_framework.mixins import RetrieveModelMixin
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import GenericViewSet
from saas_api.authentication import SaasTokenAuthentication
from saas_api.serializers.webhooks import DeliveryAttemptSerializer
from saas_api.serializers.webhooks import WebhookEventSerializer

from webhooks.models import DeliveryAttempt
from webhooks.models import WebhookEvent


class WebhookEventViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    authentication_classes = [SaasTokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = WebhookEventSerializer

    def get_queryset(self):
        return WebhookEvent.objects.filter(
            project__appid=self.kwargs["project_appid"]
        ).order_by("-created_at")


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
