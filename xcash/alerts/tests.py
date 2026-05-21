from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import httpx
from django.core.cache import cache
from django.test import TestCase
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice
from web3 import Web3

from alerts.models import ProjectAlertEventType
from alerts.models import ProjectAlertState
from alerts.models import ProjectAlertStatus
from alerts.models import ProjectTelegramAlertConfig
from alerts.service import TelegramAlertService
from chains.models import Address
from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from chains.models import Wallet
from chains.test_signer import build_test_remote_signer_backend
from core.models import PLATFORM_SETTINGS_CACHE_KEY
from core.models import PlatformSettings
from currencies.models import Crypto
from evm.models import ContractDeployCollection
from evm.models import ContractDeployCollectionStatus
from invoices.models import Invoice
from invoices.models import InvoiceBillingMode
from invoices.models import InvoicePaySlot
from invoices.models import InvoicePaySlotStatus
from users.models import Customer
from users.models import User
from users.otp import ADMIN_OTP_VERIFIED_AT_SESSION_KEY
from webhooks.models import WebhookEvent
from withdrawals.models import Withdrawal
from withdrawals.models import WithdrawalStatus

_ALERT_TEST_PATCHERS = []


def setUpModule():
    backend = build_test_remote_signer_backend()
    for target in ("chains.signer.get_signer_backend",):
        patcher = patch(target, return_value=backend)
        patcher.start()
        _ALERT_TEST_PATCHERS.append(patcher)


def tearDownModule():
    while _ALERT_TEST_PATCHERS:
        _ALERT_TEST_PATCHERS.pop().stop()


@override_settings(
    ALERTS_TELEGRAM_BOT_TOKEN="telegram-token",
    ALERTS_TELEGRAM_API_BASE="https://api.telegram.org",
    ALERTS_TELEGRAM_TIMEOUT=3.0,
    ALERTS_REPEAT_INTERVAL_MINUTES=30,
)
class TelegramAlertServiceTests(TestCase):
    def tearDown(self):
        cache.delete(PLATFORM_SETTINGS_CACHE_KEY)
        super().tearDown()

    def setUp(self):
        self.user = User.objects.create_user(username="alert-owner", password="secret")
        from projects.models import Project

        self.project = Project.objects.create(name="Alert Owner Project")
        self.config = ProjectTelegramAlertConfig.objects.create(
            project=self.project,
            telegram_chat_id="-100123456",
            created_by=self.user,
            updated_by=self.user,
        )
        self.customer = Customer.objects.create(
            project=self.project, uid="alert-customer"
        )
        self.crypto = Crypto.objects.create(
            name="Ethereum Alert",
            symbol="ETHA",
            coingecko_id="ethereum-alert",
        )
        self.chain = Chain.objects.create(
            name="Ethereum Alert",
            code="eth-alert",
            type=ChainType.EVM,
            native_coin=self.crypto,
            chain_id=402,
            rpc="http://localhost:8545",
            active=True,
        )

    def _force_verified_admin_login(self, username: str, *, verified_at=None) -> User:
        admin_user = User.objects.create_superuser(username=username, password="secret")
        device = TOTPDevice.objects.create(user=admin_user, name="Admin TOTP")
        self.client.force_login(admin_user)
        session = self.client.session
        # Admin 测试需要显式写入已验证设备，否则中间件会把会话重定向回 OTP 流程。
        session["otp_device_id"] = device.persistent_id
        session[ADMIN_OTP_VERIFIED_AT_SESSION_KEY] = (
            verified_at or timezone.now()
        ).isoformat()
        session.save()
        return admin_user

    def _create_stalled_withdrawal(self) -> Withdrawal:
        withdrawal = Withdrawal.objects.create(
            project=self.project,
            out_no="alert-withdrawal",
            chain=self.chain,
            crypto=self.crypto,
            amount=Decimal("1"),
            to="0x0000000000000000000000000000000000000012",
            status=WithdrawalStatus.REVIEWING,
        )
        Withdrawal.objects.filter(pk=withdrawal.pk).update(
            updated_at=timezone.now() - timedelta(hours=1)
        )
        withdrawal.refresh_from_db()
        return withdrawal

    def _create_stalled_webhook(self) -> WebhookEvent:
        event = WebhookEvent.objects.create(
            project=self.project,
            payload={"type": "withdrawal"},
            status=WebhookEvent.Status.PENDING,
        )
        WebhookEvent.objects.filter(pk=event.pk).update(
            created_at=timezone.now() - timedelta(hours=1)
        )
        event.refresh_from_db()
        return event

    @patch("alerts.tasks.send_project_telegram_alert.delay")
    def test_sync_operational_alerts_creates_state_and_dispatches_message(
        self, delay_mock
    ):
        withdrawal = self._create_stalled_withdrawal()

        TelegramAlertService().sync_operational_alerts()

        state = ProjectAlertState.objects.get(
            project=self.project,
            event_type=ProjectAlertEventType.WITHDRAWAL_STALLED,
            object_pk=withdrawal.pk,
        )
        self.assertEqual(state.status, ProjectAlertStatus.OPEN)
        delay_mock.assert_called_once()
        self.assertEqual(delay_mock.call_args.kwargs["mode"], "open")

    @patch("alerts.tasks.send_project_telegram_alert.delay")
    def test_sync_operational_alerts_sends_resolved_message_when_object_recovers(
        self, delay_mock
    ):
        withdrawal = self._create_stalled_withdrawal()
        service = TelegramAlertService()

        service.sync_operational_alerts()
        delay_mock.reset_mock()
        Withdrawal.objects.filter(pk=withdrawal.pk).update(
            status=WithdrawalStatus.COMPLETED
        )

        service.sync_operational_alerts()

        state = ProjectAlertState.objects.get(
            project=self.project,
            event_type=ProjectAlertEventType.WITHDRAWAL_STALLED,
            object_pk=withdrawal.pk,
        )
        self.assertEqual(state.status, ProjectAlertStatus.RESOLVED)
        self.assertEqual(delay_mock.call_args.kwargs["mode"], "resolved")

    @patch("alerts.tasks.send_project_telegram_alert.delay")
    def test_sync_operational_alerts_respects_project_subscriptions(self, delay_mock):
        self.config.notify_on_webhook_stalled = False
        self.config.save(update_fields=("notify_on_webhook_stalled",))
        self._create_stalled_webhook()

        TelegramAlertService().sync_operational_alerts()

        delay_mock.assert_not_called()

    @patch("alerts.service.httpx.post")
    def test_send_test_message_updates_verified_state(self, httpx_post_mock):
        httpx_post_mock.return_value.raise_for_status.return_value = None
        httpx_post_mock.return_value.json.return_value = {
            "ok": True,
            "result": {"message_id": 1},
        }

        TelegramAlertService().send_test_message(config_id=self.config.pk)

        self.config.refresh_from_db()
        self.assertIsNotNone(self.config.last_test_sent_at)
        self.assertIsNotNone(self.config.last_verified_at)

    def test_service_reads_repeat_interval_from_platform_settings(self):
        # 告警重复发送节流窗口应支持平台后台运行期调整，而不是固定绑定 settings。
        PlatformSettings.objects.create(alerts_repeat_interval_minutes=7)

        service = TelegramAlertService()

        self.assertEqual(service._repeat_interval, timedelta(minutes=7))

    @patch("alerts.service.httpx.post")
    def test_send_state_message_records_error_on_failure(self, httpx_post_mock):
        state = ProjectAlertState.objects.create(
            project=self.project,
            event_type=ProjectAlertEventType.WEBHOOK_STALLED,
            object_type="webhook_event",
            object_pk=100,
            fingerprint="1:webhook:100",
            severity="critical",
            title="Webhook 长时间未送达",
            detail="nonce-100 / 待投递超时",
            admin_url="/admin/webhooks/event/100/change/",
            first_seen_at=timezone.now(),
            last_seen_at=timezone.now(),
        )
        httpx_post_mock.side_effect = httpx.ConnectError("telegram down")

        from alerts.service import TelegramAlertError

        with self.assertRaises(TelegramAlertError):
            TelegramAlertService().send_state_message(state_id=state.pk, mode="open")

        state.refresh_from_db()
        self.assertIn("telegram down", state.last_error_message)

    @patch("alerts.tasks.send_project_telegram_test.delay")
    def test_project_admin_inline_send_test_view_queues_message(self, delay_mock):
        self._force_verified_admin_login("alerts-admin-2")

        response = self.client.post(
            reverse(
                "admin:alerts_projecttelegramalertconfig_send_test",
                args=[self.config.pk],
            )
        )

        self.assertRedirects(
            response,
            reverse("admin:projects_project_change", args=[self.project.pk]),
        )
        delay_mock.assert_called_once_with(config_id=self.config.pk)

    @patch("alerts.tasks.send_project_telegram_test.delay")
    def test_project_admin_inline_send_test_view_allows_expired_otp(self, delay_mock):
        self._force_verified_admin_login(
            "alerts-admin-expired",
            verified_at=timezone.now() - timedelta(minutes=16),
        )

        response = self.client.post(
            reverse(
                "admin:alerts_projecttelegramalertconfig_send_test",
                args=[self.config.pk],
            ),
        )

        self.assertRedirects(
            response,
            reverse("admin:projects_project_change", args=[self.project.pk]),
        )
        delay_mock.assert_called_once_with(config_id=self.config.pk)


@override_settings(
    ALERTS_TELEGRAM_BOT_TOKEN="telegram-token",
    ALERTS_TELEGRAM_API_BASE="https://api.telegram.org",
    ALERTS_TELEGRAM_TIMEOUT=3.0,
    ALERTS_REPEAT_INTERVAL_MINUTES=30,
)
class ContractCollectionStalledAlertTest(TestCase):
    def tearDown(self):
        cache.delete(PLATFORM_SETTINGS_CACHE_KEY)
        super().tearDown()

    def setUp(self):
        self.user = User.objects.create_user(
            username="contract-alert-owner",
            password="secret",
        )
        from projects.models import Project

        self.project = Project.objects.create(name="Contract Alert Project")
        self.config = ProjectTelegramAlertConfig.objects.create(
            project=self.project,
            telegram_chat_id="-100123457",
            created_by=self.user,
            updated_by=self.user,
        )
        self.crypto = Crypto.objects.create(
            name="Contract Alert Token",
            symbol="ALERTC",
            coingecko_id="contract-alert-token",
        )
        self.chain = Chain.objects.create(
            name="Contract Alert Chain",
            code="eth-contract-alert",
            type=ChainType.EVM,
            native_coin=self.crypto,
            chain_id=4402,
            rpc="http://localhost:8545",
            active=True,
        )

    def _make_contract_invoice(self) -> Invoice:
        invoice = Invoice.objects.create(
            project=self.project,
            out_no="contract-alert-order",
            title="Contract alert invoice",
            currency=self.crypto.symbol,
            amount=Decimal("100"),
            methods={self.crypto.symbol: [self.chain.code]},
            billing_mode=InvoiceBillingMode.CONTRACT,
            status="completed",
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        return invoice

    def _make_contract_pay_slot(self, invoice: Invoice) -> InvoicePaySlot:
        return InvoicePaySlot.objects.create(
            invoice=invoice,
            project=invoice.project,
            version=1,
            crypto=self.crypto,
            chain=self.chain,
            pay_address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000ce"
            ),
            pay_amount=Decimal("100"),
            recipient_address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000d1"
            ),
            billing_mode=InvoiceBillingMode.CONTRACT,
            status=InvoicePaySlotStatus.MATCHED,
            matched_at=timezone.now(),
        )

    def _create_collection_for_slot(
        self,
        *,
        slot: InvoicePaySlot,
        status: str,
        suffix: int,
    ) -> ContractDeployCollection:
        deployer = Address.objects.create(
            wallet=self.project.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.Vault,
            address_index=suffix,
            bip44_account=Wallet.get_bip44_account(AddressUsage.Vault),
            address=f"0x{suffix:040x}",
        )
        return ContractDeployCollection.objects.create(
            chain=self.chain,
            crypto=self.crypto,
            deployer_address=deployer,
            factory_address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000fa"
            ),
            collector_address=Web3.to_checksum_address(f"0x{(1000 + suffix):040x}"),
            recipient_address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000d1"
            ),
            salt=bytes([suffix]) * 32,
            collector_init_code_hash=bytes([suffix + 1]) * 32,
            expected_collect_value_raw=1_000_000,
            pay_slot=slot,
            status=status,
        )

    def test_stalled_contract_collections_returns_invoices_with_three_failures(self):
        from core.monitoring import OperationalRiskService

        invoice = self._make_contract_invoice()
        slot = self._make_contract_pay_slot(invoice)
        statuses = [
            ContractDeployCollectionStatus.FAILED,
            ContractDeployCollectionStatus.DROPPED,
            ContractDeployCollectionStatus.FAILED,
        ]
        for suffix, status in enumerate(statuses, start=1):
            self._create_collection_for_slot(
                slot=slot,
                status=status,
                suffix=suffix,
            )

        candidates = list(OperationalRiskService.stalled_contract_collections())

        self.assertIn(invoice, candidates)

    def test_stalled_contract_collections_excludes_active_collection(self):
        from core.monitoring import OperationalRiskService

        invoice = self._make_contract_invoice()
        slot = self._make_contract_pay_slot(invoice)
        for suffix in range(1, 4):
            self._create_collection_for_slot(
                slot=slot,
                status=ContractDeployCollectionStatus.FAILED,
                suffix=suffix,
            )
        self._create_collection_for_slot(
            slot=slot,
            status=ContractDeployCollectionStatus.BROADCASTED,
            suffix=4,
        )

        candidates = list(OperationalRiskService.stalled_contract_collections())

        self.assertNotIn(invoice, candidates)

    @patch("alerts.tasks.send_project_telegram_alert.delay")
    def test_sync_operational_alerts_creates_contract_collection_state(
        self,
        delay_mock,
    ):
        invoice = self._make_contract_invoice()
        slot = self._make_contract_pay_slot(invoice)
        for suffix in range(1, 4):
            self._create_collection_for_slot(
                slot=slot,
                status=ContractDeployCollectionStatus.FAILED,
                suffix=suffix,
            )

        TelegramAlertService().sync_operational_alerts()

        state = ProjectAlertState.objects.get(
            project=self.project,
            event_type=ProjectAlertEventType.CONTRACT_COLLECTION_STALLED,
            object_pk=invoice.pk,
        )
        self.assertEqual(state.status, ProjectAlertStatus.OPEN)
        delay_mock.assert_called_once()
