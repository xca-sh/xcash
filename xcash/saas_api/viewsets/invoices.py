from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import ModelViewSet
from saas_api.authentication import SaasTokenAuthentication
from saas_api.serializers.invoices import SaasInvoiceDetailSerializer

from common.permissions import RejectAll
from invoices.models import Invoice


class SaasInvoiceViewSet(ModelViewSet):
    """内部 Invoice API 已废弃创建能力，仅保留查询。"""

    authentication_classes = [SaasTokenAuthentication]
    permission_classes = [IsAuthenticated]
    lookup_field = "sys_no"
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        return Invoice.objects.filter(
            project__appid=self.kwargs["project_appid"]
        ).select_related("crypto", "chain", "transfer")

    def get_serializer_class(self):
        return SaasInvoiceDetailSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [RejectAll()]
