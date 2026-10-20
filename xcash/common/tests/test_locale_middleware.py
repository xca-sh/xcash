from django.conf import settings
from django.test import RequestFactory
from django.test import SimpleTestCase

from common.middlewares import XcashLocaleMiddleware


class XcashLocaleMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.middleware = XcashLocaleMiddleware(lambda request: None)
        self.factory = RequestFactory()

    def resolve(self, accept_language=None, cookie=None):
        headers = {"HTTP_ACCEPT_LANGUAGE": accept_language} if accept_language else {}
        request = self.factory.get("/", **headers)
        if cookie:
            request.COOKIES[settings.LANGUAGE_COOKIE_NAME] = cookie
        return self.middleware.resolve_language(request)

    def test_follows_first_supported_browser_language(self):
        self.assertEqual(self.resolve("zh-CN,zh;q=0.9,en;q=0.8"), "zh-hans")
        self.assertEqual(self.resolve("ru-RU,ru;q=0.9"), "ru")
        self.assertEqual(self.resolve("uk-UA,uk;q=0.9,ru;q=0.8"), "ru")

    def test_unsupported_browser_language_falls_back_to_english(self):
        self.assertEqual(self.resolve("de-DE,de;q=0.9"), "en")

    def test_request_without_accept_language_keeps_default(self):
        # 商户服务端调 API 通常不带 Accept-Language，错误文案语言保持默认
        self.assertEqual(self.resolve(), settings.LANGUAGE_CODE)

    def test_manual_choice_overrides_browser_language(self):
        self.assertEqual(self.resolve("de-DE", cookie="ru"), "ru")
        self.assertEqual(self.resolve("ru-RU", cookie="zh-hans"), "zh-hans")

    def test_invalid_cookie_is_ignored(self):
        self.assertEqual(self.resolve("ru-RU", cookie="xx"), "ru")
