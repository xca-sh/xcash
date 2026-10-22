from decimal import Decimal

from django.db.models import Count
from django.db.models import Sum
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
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
            .select_related(
                "vault_slot__customer", "vault_slot__project", "chain", "crypto"
            )
            .order_by(ordering, "-pk")
        )

    @action(detail=False, methods=["get"])
    def summary(self, request, project_appid=None):
        """项目名下仍有余额（未归集）的合约余额汇总，供 SaaS 侧边栏提示。"""
        totals = VaultSlotBalance.objects.filter(
            vault_slot__project__appid=project_appid, value__gt=0
        ).aggregate(total_worth=Sum("worth"), count=Count("pk"))
        return Response(
            {
                "total_worth": str(totals["total_worth"] or Decimal(0)),
                "count": totals["count"],
            }
        )
