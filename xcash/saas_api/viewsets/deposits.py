import re

from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin
from rest_framework.mixins import RetrieveModelMixin
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet
from saas_api.authentication import SaasTokenAuthentication
from saas_api.serializers.deposits import SaasDepositDetailSerializer

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
        """获取智能合约充值收款地址。"""
        uid = request.query_params.get("uid", "")
        chain_type = request.query_params.get("chain_type", "")
        chain_code = request.query_params.get("chain", "")
        crypto_symbol = request.query_params.get("crypto", "")

        if not uid or not UID_PATTERN.match(uid):
            raise APIError(ErrorCode.INVALID_UID)

        project = Project.retrieve(project_appid)
        if project is None:
            raise APIError(ErrorCode.PROJECT_NOT_FOUND)

        if chain_type:
            if chain_type != ChainType.EVM:
                raise APIError(ErrorCode.INVALID_CHAIN)
            chain = Chain.objects.filter(type=ChainType.EVM, active=True).first()
            if chain is None:
                raise APIError(ErrorCode.INVALID_CHAIN)
        elif chain_code:
            try:
                chain = Chain.objects.get(code=chain_code, active=True)
            except Chain.DoesNotExist:
                raise APIError(ErrorCode.INVALID_CHAIN) from None
        else:
            raise APIError(ErrorCode.INVALID_CHAIN)

        if not crypto_symbol:
            raise APIError(ErrorCode.INVALID_CRYPTO, detail="crypto is required")
        try:
            crypto = Crypto.objects.get(symbol=crypto_symbol.upper(), active=True)
        except Crypto.DoesNotExist:
            raise APIError(ErrorCode.INVALID_CRYPTO) from None

        if not crypto.active:
            raise APIError(ErrorCode.INVALID_CRYPTO)
        if not crypto.support_this_chain(chain=chain):
            raise APIError(ErrorCode.CHAIN_CRYPTO_NOT_SUPPORT)
        if not ChainProductCapabilityService.has_active_crypto_on_chain(
            chain=chain,
            crypto=crypto,
        ):
            raise APIError(ErrorCode.CHAIN_CRYPTO_NOT_SUPPORT)
        if not ChainProductCapabilityService.supports_deposit_address(
            chain=chain,
            crypto=crypto,
        ):
            raise APIError(ErrorCode.INVALID_CHAIN)
        if not project.vault_address_for_chain_type(chain.type):
            raise APIError(ErrorCode.RECIPIENT_NOT_CONFIGURED)
        customer, _ = Customer.objects.get_or_create(project=project, uid=uid)
        deposit_address = VaultSlot.ensure_deposit_address(
            chain=chain,
            customer=customer,
            crypto=crypto,
        )
        return Response({"deposit_address": deposit_address})
