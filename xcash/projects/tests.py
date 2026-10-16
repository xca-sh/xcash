import importlib
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib import admin
from django.test import TestCase
from django.test.client import RequestFactory

from chains.constants import ChainCode
from chains.models import Chain
from chains.tests_fixtures import make_evm_chain
from common.admin import ModelAdmin
from core.models import SystemSettings
from currencies.models import Crypto
from invoices.models import DifferRecipientAddress
from projects.admin import ProjectAdmin
from projects.admin import ProjectForm
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


class ProjectTestnetGateTests(TestCase):
    """主网/测试网门控：is_test 隔离两类链，防止主网与测试网代币混淆。"""

    def test_contract_receivable_chain_codes_isolate_by_is_test(self):
        make_evm_chain(code=ChainCode.Ethereum)  # 主网
        make_evm_chain(code=ChainCode.Sepolia)  # 测试网

        prod = Project.objects.create(
            name="Prod Gate Project",
            evm_vault="0x0000000000000000000000000000000000009901",
            is_test=False,
        )
        test = Project.objects.create(
            name="Test Gate Project",
            evm_vault="0x0000000000000000000000000000000000009902",
            is_test=True,
        )

        prod_codes = ProjectService.contract_receivable_chain_codes(prod)
        test_codes = ProjectService.contract_receivable_chain_codes(test)

        # 普通项目只见主网链
        self.assertIn(ChainCode.Ethereum, prod_codes)
        self.assertNotIn(ChainCode.Sepolia, prod_codes)
        # 测试项目只见测试网链
        self.assertIn(ChainCode.Sepolia, test_codes)
        self.assertNotIn(ChainCode.Ethereum, test_codes)


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


class ProjectAmlSettingsBackfillMigrationTests(TestCase):
    """projects.0003 把全局 AML 参数回填到项目的规则。

    回填函数直接跑在当前模型上；下个版本删除 SystemSettings 上的旧 AML 列时，
    本测试随之删除。
    """

    @staticmethod
    def run_backfill():
        migration = importlib.import_module(
            "projects.migrations.0003_project_aml_screening_settings"
        )
        migration.backfill_project_aml_settings_from_system(django_apps, None)

    def test_enabled_global_settings_are_copied_to_every_project(self):
        SystemSettings.objects.create(
            aml_screening_enabled=True,
            aml_screening_threshold_usd=Decimal("250"),
        )
        first = Project.objects.create(name="first")
        second = Project.objects.create(name="second")

        self.run_backfill()
        # 幂等：重复执行结果不变。
        self.run_backfill()

        for project in (first, second):
            project.refresh_from_db()
            self.assertTrue(project.aml_screening_enabled)
            self.assertEqual(project.aml_screening_threshold_usd, Decimal("250"))

    def test_disabled_global_settings_keep_project_defaults(self):
        # 未开启 AML 时全局阈值是从未动过的 0，不能搬到项目上。
        SystemSettings.objects.create(
            aml_screening_enabled=False,
            aml_screening_threshold_usd=Decimal("0"),
        )
        project = Project.objects.create(name="untouched")

        self.run_backfill()

        project.refresh_from_db()
        self.assertFalse(project.aml_screening_enabled)
        self.assertEqual(project.aml_screening_threshold_usd, Decimal("100"))

    def test_missing_global_settings_keep_project_defaults(self):
        project = Project.objects.create(name="no-settings")

        self.run_backfill()

        project.refresh_from_db()
        self.assertFalse(project.aml_screening_enabled)
        self.assertEqual(project.aml_screening_threshold_usd, Decimal("100"))
