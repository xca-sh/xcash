from django.urls import include
from django.urls import path
from rest_framework.routers import SimpleRouter
from saas_api.viewsets.currencies import SaasChainViewSet
from saas_api.viewsets.currencies import SaasCryptoViewSet
from saas_api.viewsets.projects import ProjectViewSet

router = SimpleRouter(trailing_slash=False)
router.register("projects", ProjectViewSet)
router.register("currencies", SaasCryptoViewSet, basename="saas-crypto")
router.register("cryptos", SaasCryptoViewSet, basename="saas-crypto-alias")
router.register("chains", SaasChainViewSet, basename="saas-chain")

# 嵌套在 /projects/{appid}/ 下的业务端点
from saas_api.viewsets.deposits import SaasDepositViewSet
from saas_api.viewsets.differ_addresses import SaasDifferRecipientAddressViewSet
from saas_api.viewsets.epay import EpayMerchantView
from saas_api.viewsets.invoices import SaasInvoiceViewSet
from saas_api.viewsets.stats import StatsViewSet
from saas_api.viewsets.vault_slot_balances import SaasVaultSlotBalanceViewSet
from saas_api.viewsets.webhooks import DeliveryAttemptViewSet
from saas_api.viewsets.webhooks import WebhookEventViewSet

project_router = SimpleRouter(trailing_slash=False)
project_router.register("invoices", SaasInvoiceViewSet, basename="saas-invoice")
project_router.register("deposits", SaasDepositViewSet, basename="saas-deposit")
project_router.register(
    "differ-addresses",
    SaasDifferRecipientAddressViewSet,
    basename="saas-differ-address",
)
project_router.register(
    "webhook-events", WebhookEventViewSet, basename="saas-webhook-event"
)
project_router.register(
    "delivery-attempts", DeliveryAttemptViewSet, basename="saas-delivery-attempt"
)
project_router.register("stats", StatsViewSet, basename="saas-stats")
project_router.register(
    "vault-slot-balances",
    SaasVaultSlotBalanceViewSet,
    basename="saas-vault-slot-balance",
)

app_name = "saas_api"
urlpatterns = [
    *router.urls,
    path(
        "projects/<str:project_appid>/",
        include(project_router.urls),
    ),
    # EpayMerchant 在外部视角下是项目的单例配置，不走 SimpleRouter
    path(
        "projects/<str:project_appid>/epay-merchant",
        EpayMerchantView.as_view(),
        name="saas-epay-merchant",
    ),
]
