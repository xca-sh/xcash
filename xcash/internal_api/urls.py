from django.urls import include
from django.urls import path
from internal_api.viewsets.currencies import InternalChainViewSet
from internal_api.viewsets.currencies import InternalCryptoViewSet
from internal_api.viewsets.projects import ProjectViewSet
from rest_framework.routers import SimpleRouter

router = SimpleRouter(trailing_slash=False)
router.register("projects", ProjectViewSet)
router.register("currencies", InternalCryptoViewSet, basename="internal-crypto")
router.register("chains", InternalChainViewSet, basename="internal-chain")

# 嵌套在 /projects/{appid}/ 下的业务端点
from internal_api.viewsets.deposits import InternalDepositViewSet
from internal_api.viewsets.differ_recipient_addresses import (
    DifferRecipientAddressViewSet,
)
from internal_api.viewsets.epay import EpayMerchantView
from internal_api.viewsets.invoices import InternalInvoiceViewSet
from internal_api.viewsets.operations import HotWalletFundingViewSet
from internal_api.viewsets.operations import WithdrawalReviewLogViewSet
from internal_api.viewsets.stats import StatsViewSet
from internal_api.viewsets.webhooks import DeliveryAttemptViewSet
from internal_api.viewsets.webhooks import WebhookEventViewSet
from internal_api.viewsets.withdrawals import InternalWithdrawalViewSet

project_router = SimpleRouter(trailing_slash=False)
project_router.register("invoices", InternalInvoiceViewSet, basename="internal-invoice")
project_router.register("deposits", InternalDepositViewSet, basename="internal-deposit")
project_router.register(
    "withdrawals", InternalWithdrawalViewSet, basename="internal-withdrawal"
)
project_router.register(
    "recipient-addresses",
    DifferRecipientAddressViewSet,
    basename="internal-recipient-address",
)
project_router.register(
    "hot-wallet-fundings",
    HotWalletFundingViewSet,
    basename="internal-hot-wallet-funding",
)
project_router.register(
    "withdrawal-review-logs",
    WithdrawalReviewLogViewSet,
    basename="internal-withdrawal-review-log",
)
project_router.register(
    "webhook-events", WebhookEventViewSet, basename="internal-webhook-event"
)
project_router.register(
    "delivery-attempts", DeliveryAttemptViewSet, basename="internal-delivery-attempt"
)
project_router.register("stats", StatsViewSet, basename="internal-stats")

app_name = "internal_api"
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
        name="internal-epay-merchant",
    ),
]
