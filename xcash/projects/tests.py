from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib import admin
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.test import override_settings
from django.test.client import RequestFactory
from django.urls import reverse
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from chains.constants import ChainCode
from chains.constants import ChainType
from chains.models import Chain
from common.admin import ModelAdmin
from currencies.models import Crypto
from projects.admin import DifferRecipientAddressInline
from projects.admin import ProjectAdmin
from projects.admin import ProjectForm
from projects.models import DifferRecipientAddress
from projects.models import Project
from users.models import User
from users.otp import ADMIN_OTP_VERIFIED_AT_SESSION_KEY

_PROJECT_TEST_PATCHERS = []


def setUpModule():
    # 地址派生与签名已在 chains 内部闭环，测试直接走真实派生；
    # 这里仅旁路 Chain.full_clean（避免单测连真实 RPC 校验 chain_id）。
    patcher = patch.object(Chain, "full_clean", autospec=True)
    patcher.start()
    _PROJECT_TEST_PATCHERS.append(patcher)


def tearDownModule():
    while _PROJECT_TEST_PATCHERS:
        _PROJECT_TEST_PATCHERS.pop().stop()


@override_settings(
    ALERTS_TELEGRAM_BOT_TOKEN="telegram-token",
    ALERTS_TELEGRAM_API_BASE="https://api.telegram.org",
    ALERTS_TELEGRAM_TIMEOUT=3.0,
    ALERTS_REPEAT_INTERVAL_MINUTES=30,
)
class ProjectAdminTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            username="project-owner", password="secret"
        )
        self.project = Project.objects.create(name="Owner Project")
        self.crypto = Crypto.objects.create(
            name="Ethereum Project",
            symbol="ETHP",
            coingecko_id="ethereum-project",
        )
        self.chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="http://127.0.0.1:8545",
            active=True,
        )

    def _force_verified_admin_login(self, username: str, *, verified_at=None) -> User:
        admin_user = User.objects.create_superuser(username=username, password="secret")
        device = TOTPDevice.objects.create(user=admin_user, name="Admin TOTP")
        self.client.force_login(admin_user)
        session = self.client.session
        session["otp_device_id"] = device.persistent_id
        session[ADMIN_OTP_VERIFIED_AT_SESSION_KEY] = (
            verified_at or timezone.now()
        ).isoformat()
        session.save()
        return admin_user

    def _force_verified_project_owner_login(self, *, verified_at=None) -> TOTPDevice:
        device = TOTPDevice.objects.create(user=self.user, name="Owner Admin TOTP")
        self.client.force_login(self.user)
        session = self.client.session
        session["otp_device_id"] = device.persistent_id
        session[ADMIN_OTP_VERIFIED_AT_SESSION_KEY] = (
            verified_at or timezone.now()
        ).isoformat()
        session.save()
        return device

    def _build_project_owner_request(self, *, verified_at):
        device = TOTPDevice.objects.create(user=self.user, name="Owner Admin TOTP")
        request = self.factory.post(
            reverse("admin:projects_project_change", args=[self.project.pk])
        )
        SessionMiddleware(lambda req: None).process_request(request)
        request.session["otp_device_id"] = device.persistent_id
        request.session[ADMIN_OTP_VERIFIED_AT_SESSION_KEY] = verified_at.isoformat()
        request.session.save()
        request.user = self.user
        request.user.otp_device = device
        return request

    def _build_multisig_w3(self, *, code: bytes = b"\x60", threshold=2, owners=None):
        owners = owners or [
            "0x0000000000000000000000000000000000000001",
            "0x0000000000000000000000000000000000000002",
        ]

        return SimpleNamespace(
            eth=SimpleNamespace(
                get_code=lambda address: code,
                contract=lambda address, abi: SimpleNamespace(
                    functions=SimpleNamespace(
                        getThreshold=lambda: SimpleNamespace(
                            call=lambda: threshold,
                        ),
                        getOwners=lambda: SimpleNamespace(
                            call=lambda: owners,
                        ),
                    )
                ),
            )
        )

    def test_project_admin_save_model_requires_fresh_otp_for_vault_change(
        self,
    ):
        admin_instance = ProjectAdmin(Project, admin.site)
        request = self._build_project_owner_request(
            verified_at=timezone.now() - timedelta(minutes=16)
        )
        form = SimpleNamespace(changed_data=["vault"])

        with self.assertRaises(PermissionDenied):
            admin_instance.save_model(request, self.project, form=form, change=True)

    def test_project_admin_save_model_allows_non_sensitive_change_without_fresh_otp(
        self,
    ):
        admin_instance = ProjectAdmin(Project, admin.site)
        request = self._build_project_owner_request(
            verified_at=timezone.now() - timedelta(minutes=16)
        )
        form = SimpleNamespace(changed_data=["name"])

        with (
            patch.object(
                ModelAdmin,
                "save_model",
                autospec=True,
            ) as save_model_mock,
            patch.object(
                admin_instance,
                "_require_fresh_project_change_otp",
                autospec=True,
            ) as otp_mock,
        ):
            admin_instance.save_model(request, self.project, form=form, change=True)

        otp_mock.assert_not_called()
        save_model_mock.assert_called_once()

    def test_payment_address_inline_form_validates(self):
        request = self.factory.get("/admin/projects/project/add/")
        request.user = self.user

        inline = DifferRecipientAddressInline(Project, admin.site)
        formset_class = inline.get_formset(request, self.project)
        form = formset_class.form(
            data={
                "name": "Invoice Inline",
                "chain_type": ChainType.EVM,
                "address": "0x52908400098527886E0F7030069857D2E4169EE7",
            },
            instance=DifferRecipientAddress(project=self.project),
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_project_form_accepts_contract_vault(self):
        contract_address = "0x52908400098527886E0F7030069857D2E4169EE7"
        form = ProjectForm(
            data={
                "name": self.project.name,
                "wallet": self.project.wallet_id,
                "ip_white_list": self.project.ip_white_list,
                "webhook": self.project.webhook,
                "webhook_open": self.project.webhook_open,
                "failed_count": self.project.failed_count,
                "pre_notify": self.project.pre_notify,
                "fast_confirm_threshold": self.project.fast_confirm_threshold,
                "hmac_key": self.project.hmac_key,
                "active": self.project.active,
                "vault": contract_address,
            },
            instance=self.project,
        )

        w3 = self._build_multisig_w3()
        with patch.object(Chain, "_build_w3", return_value=w3):
            self.assertTrue(form.is_valid(), form.errors)

        self.assertEqual(form.cleaned_data["vault"], contract_address)

    def test_project_form_accepts_multisig_when_address_has_code_on_one_evm_chain(
        self,
    ):
        second_chain = Chain.objects.create(
            code=ChainCode.BSC,
            rpc="http://127.0.0.1:8545",
            active=True,
        )
        contract_address = "0x52908400098527886E0F7030069857D2E4169EE7"
        form = ProjectForm(
            data={
                "name": self.project.name,
                "wallet": self.project.wallet_id,
                "ip_white_list": self.project.ip_white_list,
                "webhook": self.project.webhook,
                "webhook_open": self.project.webhook_open,
                "failed_count": self.project.failed_count,
                "pre_notify": self.project.pre_notify,
                "fast_confirm_threshold": self.project.fast_confirm_threshold,
                "hmac_key": self.project.hmac_key,
                "active": self.project.active,
                "vault": contract_address,
            },
            instance=self.project,
        )
        valid_multisig_w3 = self._build_multisig_w3()
        no_code_w3 = self._build_multisig_w3(code=b"")

        def build_w3(chain, *, force_poa=False):
            return valid_multisig_w3 if chain.pk == self.chain.pk else no_code_w3

        with patch.object(Chain, "_build_w3", autospec=True, side_effect=build_w3):
            self.assertTrue(form.is_valid(), form.errors)

        self.assertEqual(second_chain.type, ChainType.EVM)

    def test_project_form_rejects_eoa_vault(self):
        form = ProjectForm(
            data={
                "name": self.project.name,
                "wallet": self.project.wallet_id,
                "ip_white_list": self.project.ip_white_list,
                "webhook": self.project.webhook,
                "webhook_open": self.project.webhook_open,
                "failed_count": self.project.failed_count,
                "pre_notify": self.project.pre_notify,
                "fast_confirm_threshold": self.project.fast_confirm_threshold,
                "hmac_key": self.project.hmac_key,
                "active": self.project.active,
                "vault": "0x52908400098527886E0F7030069857D2E4169EE7",
            },
            instance=self.project,
        )

        w3 = self._build_multisig_w3(code=b"")
        with patch.object(Chain, "_build_w3", return_value=w3):
            self.assertFalse(form.is_valid())

        self.assertIn("vault", form.errors)

    def test_project_form_rejects_non_multisig_contract_vault(
        self,
    ):
        form = ProjectForm(
            data={
                "name": self.project.name,
                "wallet": self.project.wallet_id,
                "ip_white_list": self.project.ip_white_list,
                "webhook": self.project.webhook,
                "webhook_open": self.project.webhook_open,
                "failed_count": self.project.failed_count,
                "pre_notify": self.project.pre_notify,
                "fast_confirm_threshold": self.project.fast_confirm_threshold,
                "hmac_key": self.project.hmac_key,
                "active": self.project.active,
                "vault": "0x52908400098527886E0F7030069857D2E4169EE7",
            },
            instance=self.project,
        )

        w3 = self._build_multisig_w3(threshold=1)
        with patch.object(Chain, "_build_w3", return_value=w3):
            self.assertFalse(form.is_valid())

        self.assertIn("vault", form.errors)

    def test_project_form_rejects_changing_existing_vault(self):
        self.project.vault = "0x52908400098527886E0F7030069857D2E4169EE7"
        self.project.save(update_fields=["vault"])
        form = ProjectForm(
            data={
                "name": self.project.name,
                "wallet": self.project.wallet_id,
                "ip_white_list": self.project.ip_white_list,
                "webhook": self.project.webhook,
                "webhook_open": self.project.webhook_open,
                "failed_count": self.project.failed_count,
                "pre_notify": self.project.pre_notify,
                "fast_confirm_threshold": self.project.fast_confirm_threshold,
                "hmac_key": self.project.hmac_key,
                "active": self.project.active,
                "vault": "0x8617E340B3D01FA5F11F306F4090FD50E238070D",
            },
            instance=self.project,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("vault", form.errors)


class DifferRecipientAddressCapabilityTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Recipient Capability Project")

    def test_clean_allows_tron_recipient_address(self):
        recipient = DifferRecipientAddress(
            name="Tron Recipient",
            project=self.project,
            chain_type=ChainType.TRON,
            address="TMwFHYXLJaRUPeW6421aqXL4ZEzPRFGkGT",
        )

        recipient.clean()

    def test_invoice_recipient_queryset_returns_project_recipients(self):
        from projects.service import ProjectService

        DifferRecipientAddress.objects.create(
            name="Invoice Recipient",
            project=self.project,
            chain_type=ChainType.EVM,
            address="0x52908400098527886E0F7030069857D2E4169EE7",
        )
        recipient = DifferRecipientAddress(
            name="Other Chain Recipient",
            project=self.project,
            chain_type=ChainType.TRON,
            address="TMwFHYXLJaRUPeW6421aqXL4ZEzPRFGkGT",
        )
        recipient.save()

        qs = ProjectService.invoice_recipients(
            self.project,
            chain_type=ChainType.EVM,
        )

        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.first().chain_type, ChainType.EVM)
