from types import SimpleNamespace
from unittest.mock import patch

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.test.client import RequestFactory

from chains.constants import ChainCode
from chains.models import Chain
from chains.tests_fixtures import make_evm_chain
from common.admin import ModelAdmin
from currencies.models import Crypto
from invoices.models import DifferRecipientAddress
from projects.admin import ProjectAdmin
from projects.admin import ProjectForm
from projects.models import InvoiceReceivingMode
from projects.models import Project
from projects.service import ProjectService
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
        form = SimpleNamespace(changed_data=["evm_vault"])

        with patch.object(
            ModelAdmin,
            "save_model",
            autospec=True,
        ) as save_model_mock:
            admin_instance.save_model(request, self.project, form=form, change=True)

        save_model_mock.assert_called_once()

    def test_project_form_accepts_contract_vault(self):
        contract_address = "0x52908400098527886E0F7030069857D2E4169EE7"
        form = ProjectForm(
            data={
                "name": self.project.name,
                "ip_white_list": self.project.ip_white_list,
                "webhook": self.project.webhook,
                "webhook_open": self.project.webhook_open,
                "fast_confirm_threshold": self.project.fast_confirm_threshold,
                "aml_screening_threshold_usd": self.project.aml_screening_threshold_usd,
                "hmac_key": self.project.hmac_key,
                "active": self.project.active,
                "evm_invoice_receiving_mode": self.project.evm_invoice_receiving_mode,
                "tron_invoice_receiving_mode": self.project.tron_invoice_receiving_mode,
                "contract_receiving_enabled": True,
                "evm_vault": contract_address,
            },
            instance=self.project,
        )

        self.assertTrue(form.is_valid(), form.errors)

        self.assertEqual(form.cleaned_data["evm_vault"], contract_address)

    def test_project_form_rejects_changing_existing_vault(self):
        self.project.evm_vault = "0x52908400098527886E0F7030069857D2E4169EE7"
        self.project.save(update_fields=["evm_vault"])
        form = ProjectForm(
            data={
                "name": self.project.name,
                "ip_white_list": self.project.ip_white_list,
                "webhook": self.project.webhook,
                "webhook_open": self.project.webhook_open,
                "fast_confirm_threshold": self.project.fast_confirm_threshold,
                "aml_screening_threshold_usd": self.project.aml_screening_threshold_usd,
                "hmac_key": self.project.hmac_key,
                "active": self.project.active,
                "evm_invoice_receiving_mode": self.project.evm_invoice_receiving_mode,
                "tron_invoice_receiving_mode": self.project.tron_invoice_receiving_mode,
                "evm_vault": "0x8617E340B3D01FA5F11F306F4090FD50E238070D",
            },
            instance=self.project,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("evm_vault", form.errors)


class ProjectContractReceivingTests(TestCase):
    """智能合约收款开关的配置不变量：admin 表单经 Project.clean() 把关。"""

    EVM_VAULT = "0x52908400098527886E0F7030069857D2E4169EE7"

    def assert_clean_error(self, project: Project, field: str, message: str):
        with self.assertRaises(ValidationError) as ctx:
            project.clean()
        self.assertEqual(ctx.exception.message_dict, {field: [message]})

    def test_disable_rejected_when_vault_persisted(self):
        project = Project.objects.create(
            name="Persisted Vault",
            contract_receiving_enabled=True,
            evm_vault=self.EVM_VAULT,
        )
        project.contract_receiving_enabled = False

        # 已落库的归集地址在 admin 中是只读字段，错误必须落在开关字段上。
        self.assert_clean_error(
            project,
            "contract_receiving_enabled",
            "已设置收款归集地址，不能关闭智能合约收款。",
        )

    def test_new_vault_rejected_when_disabled(self):
        project = Project.objects.create(name="New Vault While Disabled")
        project.evm_vault = self.EVM_VAULT

        self.assert_clean_error(
            project,
            "evm_vault",
            "未开启智能合约收款，不能设置收款归集地址。",
        )

    def test_vault_slot_rejected_when_disabled(self):
        project = Project.objects.create(name="Vault Slot While Disabled")
        project.tron_invoice_receiving_mode = InvoiceReceivingMode.VaultSlot

        self.assert_clean_error(
            project,
            "tron_invoice_receiving_mode",
            "未开启智能合约收款，账单只能使用钱包直收。",
        )

    def test_vault_slot_rejected_when_chain_vault_missing(self):
        project = Project.objects.create(
            name="Vault Slot Without Vault",
            contract_receiving_enabled=True,
        )
        project.evm_invoice_receiving_mode = InvoiceReceivingMode.VaultSlot

        self.assert_clean_error(
            project,
            "evm_invoice_receiving_mode",
            "需先设置 EVM 收款归集地址。",
        )

    def test_vault_slot_allowed_when_enabled_with_vault(self):
        project = Project.objects.create(
            name="Vault Slot Ready",
            contract_receiving_enabled=True,
            evm_vault=self.EVM_VAULT,
        )
        project.evm_invoice_receiving_mode = InvoiceReceivingMode.VaultSlot

        project.clean()


class ProjectTestnetGateTests(TestCase):
    """主网/测试网门控：is_test 隔离两类链，防止主网与测试网代币混淆。"""

    def test_invoice_receivable_methods_isolate_by_is_test(self):
        mainnet = make_evm_chain(code=ChainCode.Ethereum)
        testnet = make_evm_chain(code=ChainCode.Sepolia)

        prod = Project.objects.create(
            name="Prod Gate Project",
            evm_vault="0x0000000000000000000000000000000000009901",
            evm_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
            is_test=False,
        )
        test = Project.objects.create(
            name="Test Gate Project",
            evm_vault="0x0000000000000000000000000000000000009902",
            evm_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
            is_test=True,
        )

        prod_chains = set().union(
            *ProjectService.invoice_receivable_methods(prod).values()
        )
        test_chains = set().union(
            *ProjectService.invoice_receivable_methods(test).values()
        )

        # 普通项目只见主网链，测试项目只见测试网链。
        self.assertIn(mainnet.code, prod_chains)
        self.assertNotIn(testnet.code, prod_chains)
        self.assertIn(testnet.code, test_chains)
        self.assertNotIn(mainnet.code, test_chains)


class ProjectReadinessTests(TestCase):
    def test_is_ready_reports_missing_receiving_address_when_no_differ_or_vault(self):
        project = Project.objects.create(name="No Receiving Address")

        ready, errors = project.is_ready

        self.assertFalse(ready)
        self.assertIn("钱包未配置", [str(error) for error in errors])

    def test_is_ready_does_not_report_missing_receiving_address_when_vault_exists(self):
        project = Project.objects.create(
            name="Vault Ready",
            evm_vault="0x0000000000000000000000000000000000009901",
        )

        _, errors = project.is_ready

        self.assertNotIn("钱包未配置", [str(error) for error in errors])

    def test_is_ready_does_not_report_missing_receiving_address_when_differ_exists(
        self,
    ):
        project = Project.objects.create(name="Differ Ready")
        DifferRecipientAddress.objects.create(
            project=project,
            chain_type="evm",
            address="0x0000000000000000000000000000000000009901",
        )

        _, errors = project.is_ready

        self.assertNotIn("钱包未配置", [str(error) for error in errors])
