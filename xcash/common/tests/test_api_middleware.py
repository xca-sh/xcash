import json
from uuid import uuid4

from django.test import RequestFactory
from django.test import TestCase
from django.test import override_settings
from django.utils import timezone
from web3 import Web3

from common.consts import APPID_HEADER
from common.consts import NONCE_HEADER
from common.consts import SIGNATURE_HEADER
from common.consts import TIMESTAMP_HEADER
from common.crypto import calc_hmac
from common.error_codes import ErrorCode
from common.middlewares import XcashMiddleware
from projects.models import Project


@override_settings(DEBUG=False)
class XcashApiMiddlewareTests(TestCase):
    """商户 /v1 API 准入链：项目 → IP 白名单 → HMAC 签名 → nonce 防重放。"""

    def setUp(self):
        self.factory = RequestFactory()
        self.project = Project.objects.create(
            name="Api Gate Project",
            webhook="https://merchant.example.com/hook",
            ip_white_list="127.0.0.1",
            evm_vault=Web3.to_checksum_address("0x" + "a1" * 20),
        )
        self.passed = object()
        self.middleware = XcashMiddleware(lambda _request: self.passed)

    def signed_request(self, *, remote_addr="127.0.0.1", signature=None, nonce=None):
        body = json.dumps({"out_no": "gate-1"})
        timestamp = str(int(timezone.now().timestamp()))
        nonce = uuid4().hex if nonce is None else nonce
        headers = {
            APPID_HEADER: self.project.appid,
            TIMESTAMP_HEADER: timestamp,
            NONCE_HEADER: nonce,
            SIGNATURE_HEADER: signature
            or calc_hmac(
                message=f"{nonce}{timestamp}{body}", key=self.project.hmac_key
            ),
        }
        return self.factory.post(
            "/v1/invoice",
            data=body,
            content_type="application/json",
            headers=headers,
            REMOTE_ADDR=remote_addr,
        )

    def assert_rejected(self, response, error: ErrorCode):
        self.assertIsNot(response, self.passed)
        self.assertEqual(json.loads(response.content)["code"], error.value.code)

    def test_signed_request_from_whitelisted_ip_passes_and_replay_is_rejected(self):
        nonce = uuid4().hex

        self.assertIs(self.middleware(self.signed_request(nonce=nonce)), self.passed)
        self.assert_rejected(
            self.middleware(self.signed_request(nonce=nonce)),
            ErrorCode.REPLAY_ATTACK,
        )

    def test_ip_whitelist_is_checked_before_signature(self):
        # 白名单外的来源即使签名正确也必须拒绝，且不应消耗 nonce。
        nonce = uuid4().hex

        self.assert_rejected(
            self.middleware(self.signed_request(remote_addr="10.9.9.9", nonce=nonce)),
            ErrorCode.IP_FORBIDDEN,
        )
        self.assertIs(self.middleware(self.signed_request(nonce=nonce)), self.passed)

    def test_bad_signature_is_rejected(self):
        self.assert_rejected(
            self.middleware(self.signed_request(signature="0" * 64)),
            ErrorCode.SIGNATURE_ERROR,
        )

    def test_missing_nonce_is_rejected(self):
        self.assert_rejected(
            self.middleware(self.signed_request(nonce="")),
            ErrorCode.PARAMETER_ERROR,
        )

    def test_unknown_appid_is_rejected(self):
        request = self.signed_request()
        request.META["HTTP_XC_APPID"] = "XC-unknown"

        self.assert_rejected(self.middleware(request), ErrorCode.INVALID_APPID)
