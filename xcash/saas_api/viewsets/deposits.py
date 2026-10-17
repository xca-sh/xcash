from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin
from rest_framework.mixins import RetrieveModelMixin
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet
from saas_api.authentication import SaasTokenAuthentication
from saas_api.serializers.deposits import SaasDepositDetailSerializer

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
        return (
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
