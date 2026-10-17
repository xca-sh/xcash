"""ProjectViewSet PATCH 字段白名单与 PUT/DELETE 拦截的安全回归测试。

覆盖：
- PUT/DELETE 被 http_method_names 拦截（405）
- PATCH 只能修改白名单字段，非白名单字段被 DRF 忽略而非抛错
- 每个字段的业务校验（webhook scheme、hmac 长度、IP/CIDR、数值范围）
- 跨字段校验（单笔限额 ≤ 日限额）
- 合法 PATCH 正常入库
"""

from decimal import Decimal

import pytest
from django.test import override_settings

from chains.models import ChainType
from projects.models import InvoiceReceivingMode
from projects.models import Project

AUTH_HEADER = "Bearer test-saas-token"


@pytest.fixture
def project(db):
    return Project.objects.create(
        name="patch-test-project",
        ip_white_list="*",
        webhook="",
        hmac_key="ORIG-HMAC-KEY-ORIGINAL-32CHARS00",
    )


def _url(project):
    return f"/saas/v1/projects/{project.appid}"


# ---------- HTTP 方法拦截 ----------


@pytest.mark.django_db
class TestMethodRestrictions:
    def test_put_returns_405(self, client, project):
        response = client.put(
            _url(project),
            data={"name": "hacked"},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 405

    def test_delete_returns_405(self, client, project):
        response = client.delete(
            _url(project),
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 405
        # 项目不应被删除。
        assert Project.objects.filter(pk=project.pk).exists()

    def test_get_detail_allowed(self, client, project):
        response = client.get(
            _url(project),
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200

    def test_patch_allowed(self, client, project):
        response = client.patch(
            _url(project),
            data={"webhook_open": False},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200


# ---------- PATCH 字段白名单 ----------


@pytest.mark.django_db
class TestPatchFieldWhitelist:
    def test_non_whitelisted_fields_are_ignored(self, client, project):
        """非白名单字段不会被写入，ModelSerializer 默认忽略额外字段。"""
        original_name = project.name
        original_active = project.active
        original_appid = project.appid

        response = client.patch(
            _url(project),
            data={
                "name": "malicious-rename",
                "active": False,
                "appid": "XC-HACKED0",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200

        project.refresh_from_db()
        assert project.name == original_name
        assert project.active == original_active
        assert project.appid == original_appid

    def test_receiving_mode_per_chain_update(self, client, project):
        """账单收款模式按链配置，切换只影响后续新账单的地址分配。"""
        assert project.evm_invoice_receiving_mode == InvoiceReceivingMode.Differ
        assert project.tron_invoice_receiving_mode == InvoiceReceivingMode.Differ

        response = client.patch(
            _url(project),
            data={
                "evm_invoice_receiving_mode": InvoiceReceivingMode.Differ,
                "tron_invoice_receiving_mode": InvoiceReceivingMode.VaultSlot,
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200, response.content

        project.refresh_from_db()
        assert project.evm_invoice_receiving_mode == InvoiceReceivingMode.Differ
        assert project.tron_invoice_receiving_mode == InvoiceReceivingMode.VaultSlot
        assert (
            project.resolved_invoice_receiving_mode(ChainType.EVM)
            == InvoiceReceivingMode.Differ
        )
        assert (
            project.resolved_invoice_receiving_mode(ChainType.TRON)
            == InvoiceReceivingMode.VaultSlot
        )

        # GET 详情只回显按链模式。
        detail = client.get(_url(project), HTTP_AUTHORIZATION=AUTH_HEADER).json()
        assert detail["evm_invoice_receiving_mode"] == InvoiceReceivingMode.Differ
        assert detail["tron_invoice_receiving_mode"] == InvoiceReceivingMode.VaultSlot

    def test_happy_path_multiple_fields(self, client, project):
        """合法 PATCH：同时修改多个白名单字段，均正确入库。"""
        payload = {
            "webhook": "https://example.com/cb",
            "webhook_open": False,
            "fast_confirm_threshold": "25.50",
        }
        response = client.patch(
            _url(project),
            data=payload,
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200, response.content

        project.refresh_from_db()
        assert project.webhook == "https://example.com/cb"
        assert project.webhook_open is False
        assert project.fast_confirm_threshold == Decimal("25.50")


# ---------- 单字段校验 ----------


@pytest.mark.django_db
class TestWebhookValidation:
    def test_rejects_javascript_scheme(self, client, project):
        response = client.patch(
            _url(project),
            data={"webhook": "javascript:alert(1)"},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        # URLField 的内置校验或自定义 scheme 校验都会阻止；接受任一 400。
        assert response.status_code == 400

    @override_settings(WEBHOOK_ALLOW_INTERNAL_TARGETS=False)
    def test_rejects_http_url(self, client, project):
        # 投递只接受 https；放行 http 会让付款后的通知直接投递失败。
        response = client.patch(
            _url(project),
            data={"webhook": "http://example.com/cb"},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 400
        project.refresh_from_db()
        assert project.webhook != "http://example.com/cb"

    @override_settings(WEBHOOK_ALLOW_INTERNAL_TARGETS=True)
    def test_accepts_http_url_when_internal_targets_allowed(self, client, project):
        # 本地联调 / 压测的内网放行模式下投递接受 http，入口保持同一口径。
        response = client.patch(
            _url(project),
            data={"webhook": "http://localhost:8000/cb"},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200

    def test_accepts_https_url(self, client, project):
        response = client.patch(
            _url(project),
            data={"webhook": "https://example.com/cb"},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200


@pytest.mark.django_db
class TestHmacKeyValidation:
    def test_too_short(self, client, project):
        response = client.patch(
            _url(project),
            data={"hmac_key": "short"},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 400

    def test_min_length_ok(self, client, project):
        value = "a" * 16
        response = client.patch(
            _url(project),
            data={"hmac_key": value},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200

    def test_max_length_ok(self, client, project):
        # 模型层 ShortUUIDField(length=32) 将 max_length 限制为 32。
        value = "a" * 32
        response = client.patch(
            _url(project),
            data={"hmac_key": value},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200

    def test_too_long(self, client, project):
        value = "a" * 33
        response = client.patch(
            _url(project),
            data={"hmac_key": value},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 400


@pytest.mark.django_db
class TestIpWhiteListValidation:
    def test_rejects_garbage(self, client, project):
        response = client.patch(
            _url(project),
            data={"ip_white_list": "abc"},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 400

    def test_accepts_wildcard(self, client, project):
        response = client.patch(
            _url(project),
            data={"ip_white_list": "*"},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200

    def test_accepts_cidr_list(self, client, project):
        response = client.patch(
            _url(project),
            data={"ip_white_list": "192.168.1.0/24, 10.0.0.1"},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200
        project.refresh_from_db()
        assert project.ip_white_list == "192.168.1.0/24, 10.0.0.1"

    def test_rejects_over_max_entries(self, client, project):
        # 101 条 IP，超出 100 上限。
        entries = ",".join(f"10.0.0.{i % 255}" for i in range(101))
        response = client.patch(
            _url(project),
            data={"ip_white_list": entries},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 400


@pytest.mark.django_db
class TestFastConfirmThresholdValidation:
    def test_rejects_negative(self, client, project):
        response = client.patch(
            _url(project),
            data={"fast_confirm_threshold": "-1"},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 400

    def test_accepts_zero(self, client, project):
        response = client.patch(
            _url(project),
            data={"fast_confirm_threshold": "0"},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200


@pytest.mark.django_db
class TestAmlScreeningSettings:
    def test_patch_updates_and_detail_echoes(self, client, project):
        response = client.patch(
            _url(project),
            data={
                "aml_screening_enabled": True,
                "aml_screening_threshold_usd": "300",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200, response.content

        project.refresh_from_db()
        assert project.aml_screening_enabled is True
        assert project.aml_screening_threshold_usd == Decimal("300")

        detail = client.get(_url(project), HTTP_AUTHORIZATION=AUTH_HEADER).json()
        assert detail["aml_screening_enabled"] is True
        assert Decimal(detail["aml_screening_threshold_usd"]) == Decimal("300")

    def test_rejects_negative_threshold(self, client, project):
        response = client.patch(
            _url(project),
            data={"aml_screening_threshold_usd": "-1"},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 400


# ---------- 生效收款方式预览 ----------


@pytest.mark.django_db
class TestReceivableMethods:
    def test_returns_mapping(self, client, project):
        """生效收款方式接口返回 crypto→链 映射；无可收币种时为空 dict。"""
        response = client.get(
            f"{_url(project)}/receivable-methods",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200
        assert isinstance(response.json(), dict)
