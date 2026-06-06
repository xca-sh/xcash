import re

from internal_api.authentication import InternalTokenAuthentication
from internal_api.serializers.deposits import InternalDepositDetailSerializer
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin
from rest_framework.mixins import RetrieveModelMixin
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from chains.capabilities import ChainProductCapabilityService
from chains.constants import ChainType
from chains.models import Chain
from chains.models import VaultSlot
from common.error_codes import ErrorCode
from common.exceptions import APIError
from currencies.models import Crypto
from deposits.models import Deposit
from projects.models import Customer
from projects.models import Project

UID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")


class InternalDepositViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    authentication_classes = [InternalTokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = InternalDepositDetailSerializer
    lookup_field = "sys_no"

    def get_queryset(self):
        return (
            Deposit.objects.filter(
                customer__project__appid=self.kwargs["project_appid"]
            )
            .select_related("customer", "transfer__crypto", "transfer__chain")
            .order_by("-created_at", "-pk")
        )

    @action(detail=False, methods=["get"])
    def address(self, request, project_appid=None):
        """获取 VaultSlot 充币地址。"""
        uid = request.query_params.get("uid", "")
        chain_type = request.query_params.get("chain_type", "")
        chain_code = request.query_params.get("chain", "")

        if not uid or not UID_PATTERN.match(uid):
            raise APIError(ErrorCode.INVALID_UID)

        project = Project.retrieve(project_appid)
        if project is None:
            raise APIError(ErrorCode.PROJECT_NOT_FOUND)

        if chain_type:
            if chain_type != ChainType.EVM:
                raise APIError(ErrorCode.INVALID_CHAIN)
            chain = Chain.objects.filter(
                type=ChainType.EVM, active=True
            ).first()
            if chain is None:
                raise APIError(ErrorCode.INVALID_CHAIN)
        elif chain_code:
            try:
                chain = Chain.objects.get(code=chain_code, active=True)
            except Chain.DoesNotExist:
                raise APIError(ErrorCode.INVALID_CHAIN) from None
        else:
            raise APIError(ErrorCode.INVALID_CHAIN)

        customer, _ = Customer.objects.get_or_create(project=project, uid=uid)
        if chain.type == ChainType.TRON:
            try:
                usdt = Crypto.objects.get(symbol="USDT", active=True)
            except Crypto.DoesNotExist:
                raise APIError(ErrorCode.INVALID_CRYPTO) from None
            if not ChainProductCapabilityService.supports_deposit_address(
                chain=chain,
                crypto=usdt,
            ):
                raise APIError(ErrorCode.INVALID_CHAIN)
        deposit_address = VaultSlot.ensure_deposit_address(
            chain=chain,
            customer=customer,
            expose_native=chain.type == ChainType.EVM,
        )
        return Response({"deposit_address": deposit_address})
