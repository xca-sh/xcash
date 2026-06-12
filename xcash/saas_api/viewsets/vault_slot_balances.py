from rest_framework.mixins import ListModelMixin
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import GenericViewSet
from saas_api.authentication import SaasTokenAuthentication
from saas_api.serializers.vault_slot_balances import SaasVaultSlotBalanceSerializer

from chains.models import VaultSlotBalance


class SaasVaultSlotBalanceViewSet(ListModelMixin, GenericViewSet):
    """SaaS 项目名下 VaultSlot 合约余额快照。"""

    authentication_classes = [SaasTokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = SaasVaultSlotBalanceSerializer

    def get_queryset(self):
        return (
            VaultSlotBalance.objects.filter(
                vault_slot__project__appid=self.kwargs["project_appid"]
            )
            .select_related("vault_slot__customer", "chain", "crypto")
            .order_by("-updated_at", "-pk")
        )
