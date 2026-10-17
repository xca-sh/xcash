from django.test import RequestFactory
from django.test import TestCase
from django.utils import timezone
from web3 import Web3

from common.consts import APPID_HEADER
from common.consts import TIMESTAMP_HEADER
from common.error_codes import ErrorCode
from common.middlewares import XcashMiddleware
from projects.models import Project


class ProjectActiveGateTests(TestCase):
    """停用项目必须在鉴权阶段被拒绝。

    active 此前只有 SaaS 的 activate/deactivate 会写、没有任何读取方：运维或 SaaS
    停用商户后，对方仍能照常建账单、申请充币地址、占用 VaultSlot 并消耗归集 gas，
    后台却显示「已停用」。它与 SaaS 侧的 frozen 是两套独立状态，互相兜不住。
    """

    def setUp(self):
        self.factory = RequestFactory()

    def make_ready_project(self, **kwargs):
        # 必须配到 is_ready 通过，否则两个用例都会先被「项目未配置」拦下，
        # active 就不再是唯一变量，测试会因错误的原因通过。
        defaults = {
            "name": f"ActiveGate-{Project.objects.count()}",
            "webhook": "https://merchant.example.com/hook",
            "ip_white_list": "*",
            "evm_vault": Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000A1"
            ),
        }
        defaults.update(kwargs)
        return Project.objects.create(**defaults)

    def call_middleware(self, project):
        # 带上合法时间戳，否则会先被时间戳校验拦下，测不到 active 门禁。
        request = self.factory.get(
            "/v1/deposit/address",
            headers={
                APPID_HEADER.lower(): project.appid,
                TIMESTAMP_HEADER.lower(): str(int(timezone.now().timestamp())),
            },
        )
        middleware = XcashMiddleware(lambda _request: None)
        return middleware.check_project(request)

    def test_deactivated_project_is_rejected(self):
        project = self.make_ready_project(active=False)

        rejection = self.call_middleware(project)

        self.assertIsNotNone(rejection)
        self.assertEqual(
            rejection.to_response().status_code, ErrorCode.ACCESS_DENY.value.status
        )

    def test_active_project_passes_project_gate(self):
        project = self.make_ready_project(active=True)

        self.assertIsNone(self.call_middleware(project))
