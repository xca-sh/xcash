from rest_framework import serializers
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet
from saas_api.authentication import SaasTokenAuthentication

from chains.models import VaultSlot
from chains.vault_slots import schedule_refresh_after_manual_operation
from common.error_codes import ErrorCode
from common.exceptions import APIError


class VaultSlotRefreshSerializer(serializers.Serializer):
    chain = serializers.CharField()
    address = serializers.CharField()
    # 商户本次发出的部署交易哈希（可选）：引擎据此补建部署前原生币入账，自行读回执校验
    deploy_tx_hash = serializers.RegexField(
        r"^(0x)?[0-9a-fA-F]{64}$", required=False, allow_blank=True
    )


class SaasVaultSlotViewSet(GenericViewSet):
    """SaaS 项目名下 VaultSlot 的操作端点。"""

    authentication_classes = [SaasTokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = VaultSlotRefreshSerializer

    @action(detail=False, methods=["post"])
    def refresh(self, request, project_appid=None):
        """商户用自己的钱包部署 / 归集后通知引擎：60 秒后按链上真值刷新部署状态与余额。"""
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            raise APIError(ErrorCode.PARAMETER_ERROR)
        slot = VaultSlot.objects.filter(
            project__appid=project_appid,
            chain__code=serializer.validated_data["chain"],
            address=serializer.validated_data["address"],
        ).first()
        if slot is None:
            raise APIError(ErrorCode.VAULT_SLOT_NOT_FOUND)
        schedule_refresh_after_manual_operation(
            slot,
            deploy_tx_hash=serializer.validated_data.get("deploy_tx_hash") or None,
        )
        return Response({"scheduled": True}, status=status.HTTP_202_ACCEPTED)
