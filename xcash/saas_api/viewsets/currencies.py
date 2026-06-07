from rest_framework.mixins import ListModelMixin
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import GenericViewSet
from saas_api.authentication import SaasTokenAuthentication
from saas_api.serializers.currencies import SaasChainSerializer
from saas_api.serializers.currencies import SaasCryptoSerializer

from chains.models import Chain
from currencies.models import Crypto


class SaasCryptoViewSet(ListModelMixin, GenericViewSet):
    authentication_classes = [SaasTokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = SaasCryptoSerializer
    queryset = Crypto.objects.filter(active=True).prefetch_related(
        "crypto_on_chains__chain"
    )
    pagination_class = None


class SaasChainViewSet(ListModelMixin, GenericViewSet):
    authentication_classes = [SaasTokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = SaasChainSerializer
    queryset = Chain.objects.filter(active=True)
    pagination_class = None
