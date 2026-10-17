from django.test import RequestFactory
from django.test import SimpleTestCase

from common.middlewares import XcashMiddleware


class PublicEndpointExemptionTests(SimpleTestCase):
    """支付页公开端点中间件豁免：无 appid / 签名即可访问，且豁免不得误扩大到商户端点。

    光在 DRF view 设 AllowAny 不够——XcashMiddleware 对所有 /v1/ 默认强制 appid+签名，
    公开端点必须显式落在白名单（_is_no_signature_request）里，否则会被拦成 INVALID_APPID。
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = XcashMiddleware(lambda request: None)

    def test_metadata_get_is_public(self):
        # /v1/metadata 公开只读：既不需要 project（appid），也不需要 HMAC 签名。
        request = self.factory.get("/v1/metadata")
        self.assertTrue(XcashMiddleware._is_no_signature_request(request))
        self.assertFalse(self.middleware._requires_project(request))
        self.assertFalse(self.middleware._requires_project(request))

    def test_invoice_public_retrieve_stays_public(self):
        # 固定既有豁免（invoice 公开详情）不被回归破坏。
        request = self.factory.get("/v1/invoice/INV123")
        self.assertFalse(self.middleware._requires_project(request))

    def test_merchant_endpoint_still_requires_project(self):
        # 商户端点（非白名单）仍必须带 appid + 签名，确保豁免未误扩大。
        request = self.factory.get("/v1/deposit/address")
        self.assertTrue(self.middleware._requires_project(request))
        self.assertTrue(self.middleware._requires_project(request))

    def test_metadata_post_not_exempted(self):
        # 仅 GET 豁免；POST /v1/metadata 不在白名单，防止豁免被方法混用绕过。
        request = self.factory.post("/v1/metadata")
        self.assertFalse(XcashMiddleware._is_no_signature_request(request))
