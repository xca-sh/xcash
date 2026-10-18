from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin
from rest_framework.mixins import RetrieveModelMixin
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet
from saas_api.authentication import SaasTokenAuthentication
from saas_api.list_filters import deposit_keyword_condition
from saas_api.list_filters import filter_chain_crypto
from saas_api.list_filters import filter_created_range
from saas_api.list_filters import keyword_param
from saas_api.serializers.deposits import SaasDepositDetailSerializer

from chains.models import TransferStatus
from common.error_codes import ErrorCode
from common.exceptions import APIError
from deposits.models import Deposit
from deposits.service import DepositService
from projects.models import Project


class SaasDepositViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    authentication_classes = [SaasTokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = SaasDepositDetailSerializer
    lookup_field = "sys_no"

    def get_queryset(self):
        queryset = (
            Deposit.objects.filter(
                customer__project__appid=self.kwargs["project_appid"]
            )
            .select_related(
                "customer",
                "transfer__crypto",
                "transfer__chain",
                "transfer__aml_assessment",
            )
            .order_by("-created_at", "-pk")
        )
        # 仅列表接受筛选；详情按 sys_no 精确取，不受筛选参数影响。
        # 直接实例化 viewset（无 action 属性）时也按非列表处理。
        if getattr(self, "action", None) == "list":
            queryset = self.filter_list(queryset)
        return queryset

    def filter_list(self, queryset):
        """商户中心充值列表筛选：状态、关键字、公链/币种、创建时间区间，可任意组合。

        充值没有独立状态机，状态即其链上转账的确认状态（confirming / confirmed）。
        """
        params = self.request.query_params
        status = params.get("status", "").strip()
        if status:
            if status not in TransferStatus.values:
                raise APIError(
                    ErrorCode.PARAMETER_ERROR,
                    f"status 只能是 {' / '.join(TransferStatus.values)}",
                )
            queryset = queryset.filter(transfer__status=status)
        keyword = keyword_param(params)
        if keyword:
            queryset = queryset.filter(deposit_keyword_condition(keyword))
        queryset = filter_chain_crypto(
            queryset,
            params,
            chain_field="transfer__chain",
            crypto_field="transfer__crypto",
        )
        return filter_created_range(queryset, params)

    @action(detail=False, methods=["get"])
    def address(self, request, project_appid=None):
        """获取智能合约充值收款地址，校验口径与 /v1 商户接口一致。"""
        project = Project.retrieve(project_appid)
        if project is None:
            raise APIError(ErrorCode.PROJECT_NOT_FOUND)

        deposit_address, _chain, _crypto = DepositService.resolve_deposit_address(
            project=project,
            uid=request.query_params.get("uid", ""),
            chain_code=request.query_params.get("chain", ""),
            crypto_symbol=request.query_params.get("crypto", "").upper(),
        )
        return Response({"deposit_address": deposit_address})
