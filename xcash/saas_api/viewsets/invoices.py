from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import ModelViewSet
from saas_api.authentication import SaasTokenAuthentication
from saas_api.invoice_status import DISPLAY_STATUS_FILTERS
from saas_api.serializers.invoices import SaasInvoiceDetailSerializer

from common.error_codes import ErrorCode
from common.exceptions import APIError
from common.permissions import RejectAll
from invoices.models import Invoice


class SaasInvoiceViewSet(ModelViewSet):
    """内部 Invoice API 已废弃创建能力，仅保留查询。"""

    authentication_classes = [SaasTokenAuthentication]
    permission_classes = [IsAuthenticated]
    lookup_field = "sys_no"
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        queryset = Invoice.objects.filter(
            project__appid=self.kwargs["project_appid"]
        ).select_related(
            "crypto",
            "chain",
            # TransferSerializer 读 tx.chain / tx.crypto，confirm_progress 也要读
            # transfer.chain；不预加载时列表每行各多两次查询。
            "transfer__chain",
            "transfer__crypto",
            "transfer__aml_assessment",
        )
        if self.action == "list":
            queryset = self.filter_by_display_status(queryset)
        return queryset

    def filter_by_display_status(self, queryset):
        """按商户可见状态筛选，口径与 stats/invoice-status-counts 一致。"""
        status = self.request.query_params.get("status")
        if not status:
            return queryset
        condition = DISPLAY_STATUS_FILTERS.get(status)
        if condition is None:
            raise APIError(
                ErrorCode.PARAMETER_ERROR,
                f"status 只能是 {' / '.join(DISPLAY_STATUS_FILTERS)}",
            )
        return queryset.filter(condition)

    def get_serializer_class(self):
        return SaasInvoiceDetailSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [RejectAll()]
