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
    ordering_fields = {"updated_at", "worth"}

    def get_queryset(self):
        ordering = self.request.query_params.get("ordering") or "-worth"
        if ordering.lstrip("-") not in self.ordering_fields:
            ordering = "-worth"
        return (
            VaultSlotBalance.objects.filter(
                vault_slot__project__appid=self.kwargs["project_appid"]
            )
            .select_related("vault_slot__customer", "chain", "crypto")
            .order_by(ordering, "-pk")
        )
