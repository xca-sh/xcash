from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import ModelViewSet
from saas_api.authentication import SaasTokenAuthentication
from saas_api.csv_export import csv_response
from saas_api.csv_export import escape_formula
from saas_api.csv_export import iso
from saas_api.invoice_status import DISPLAY_STATUS_FILTERS
from saas_api.invoice_status import display_status
from saas_api.list_filters import filter_invoices
from saas_api.serializers.invoices import SaasInvoiceDetailSerializer

from common.error_codes import ErrorCode
from common.exceptions import APIError
from common.permissions import RejectAll
from common.utils.math import format_decimal_stripped
from invoices.models import Invoice

EXPORT_COLUMNS = [
    "sys_no",
    "out_no",
    "title",
    "status",
    "currency",
    "amount",
    "crypto",
    "chain",
    "pay_amount",
    "pay_address",
    "worth",
    "tx_hash",
    "from_address",
    "risk_level",
    "created_at",
    "expires_at",
]


def export_row(invoice: Invoice) -> list:
    transfer = invoice.transfer
    assessment = getattr(transfer, "aml_assessment", None) if transfer else None
    return [
        invoice.sys_no,
        escape_formula(invoice.out_no),
        escape_formula(invoice.title),
        display_status(invoice),
        invoice.currency_id,
        format_decimal_stripped(invoice.amount),
        invoice.crypto.symbol if invoice.crypto else "",
        invoice.chain.code if invoice.chain else "",
        format_decimal_stripped(invoice.pay_amount),
        escape_formula(invoice.pay_address),
        format_decimal_stripped(invoice.worth),
        escape_formula(transfer.hash if transfer else ""),
        escape_formula(transfer.from_address if transfer else ""),
        assessment.risk_level if assessment else "",
        iso(invoice.created_at),
        iso(invoice.expires_at),
    ]


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
        if self.action in ("list", "export"):
            # 状态、关键字、公链/币种、创建时间区间可任意组合；导出与列表同一口径
            queryset = self.filter_by_display_status(queryset)
            queryset = filter_invoices(queryset, self.request.query_params)
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

    @action(detail=False, methods=["get"])
    def export(self, request, project_appid=None):
        """按与列表相同的筛选条件导出 CSV，不分页，超过行数上限返回 400。"""
        queryset = self.get_queryset().order_by("-created_at", "-pk")
        return csv_response(
            queryset,
            columns=EXPORT_COLUMNS,
            to_row=export_row,
            filename_prefix="invoices",
        )

    def get_serializer_class(self):
        return SaasInvoiceDetailSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve", "export"):
            return [IsAuthenticated()]
        return [RejectAll()]
