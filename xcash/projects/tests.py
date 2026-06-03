from types import SimpleNamespace
from unittest.mock import patch

from django.contrib import admin
from django.test import TestCase
from django.test.client import RequestFactory

from chains.constants import ChainCode
from chains.constants import ChainType
from chains.models import Chain
from common.admin import ModelAdmin
from currencies.models import Crypto
from invoices.models import DifferRecipientAddress
from projects.admin import DifferRecipientAddressInline
from projects.admin import ProjectAdmin
from projects.admin import ProjectForm
from projects.models import Project
from users.models import User

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

    def _force_admin_login(self, username: str) -> User:
        admin_user = User.objects.create_superuser(username=username, password="secret")
        self.client.force_login(admin_user)
        return admin_user

    def _build_project_owner_request(self):
        request = self.factory.post("/admin/projects/project/")
        request.user = self.user
        return request

    def test_project_admin_save_model_allows_vault_change(
        self,
    ):
        admin_instance = ProjectAdmin(Project, admin.site)
        request = self._build_project_owner_request()
        form = SimpleNamespace(changed_data=["vault"])

        with patch.object(
            ModelAdmin,
            "save_model",
            autospec=True,
        ) as save_model_mock:
            admin_instance.save_model(request, self.project, form=form, change=True)

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

        self.assertTrue(form.is_valid(), form.errors)

        self.assertEqual(form.cleaned_data["vault"], contract_address)

    def test_project_form_rejects_changing_existing_vault(self):
        self.project.vault = "0x52908400098527886E0F7030069857D2E4169EE7"
        self.project.save(update_fields=["vault"])
        form = ProjectForm(
            data={
                "name": self.project.name,
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
