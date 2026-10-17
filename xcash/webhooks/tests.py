import socket
import time
from unittest.mock import MagicMock
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase
from django.test import TestCase
from django.test import override_settings
from django.utils import timezone

from core.models import SYSTEM_SETTINGS_CACHE_KEY
from core.models import SystemSettings
from projects.models import Project
from webhooks.models import DeliveryAttempt
from webhooks.models import WebhookEvent
from webhooks.service import WebhookService
from webhooks.tasks import DeliveryTargetResolutionError
from webhooks.tasks import _claim_event_for_delivery
from webhooks.tasks import deliver_event
from webhooks.tasks import next_backoff
from webhooks.tasks import pin_request_to_ip
from webhooks.tasks import reap_stalled_events
from webhooks.tasks import resolve_addresses
from webhooks.tasks import safe_delivery_target
from webhooks.tasks import schedule_events


def _make_project(**kwargs):
    defaults = {
        "name": f"Demo-{Project.objects.count()}",
        "webhook": "https://93.184.216.34/hook",
        "webhook_open": True,
    }
    defaults.update(kwargs)
    return Project.objects.create(**defaults)


class WebhookServiceTests(TestCase):
    def tearDown(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        super().tearDown()

    @patch("webhooks.tasks.deliver_event.delay")
    def test_create_event_enqueues_delivery_after_commit(self, deliver_event_mock):
        # webhook 事件创建后必须显式在 on_commit 派发投递任务，而不是依赖 model signal。
        project = _make_project()

        with self.captureOnCommitCallbacks(execute=True):
            event = WebhookService.create_event(
                project=project,
                payload={"type": "deposit", "data": {"foo": "bar"}},
            )

        deliver_event_mock.assert_called_once_with(event.pk)

    def test_next_backoff_uses_system_settings_cap(self):
        # Webhook 退避上限应可由系统参数中心调整，避免固定 120 秒无法匹配实际值守策略。
        SystemSettings.objects.create(webhook_delivery_max_backoff_seconds=20)

        self.assertEqual(next_backoff(1), 4)
        self.assertEqual(next_backoff(10), 20)


class DeliverEventTests(TestCase):
    """覆盖 deliver_event 各核心分支。"""

    def tearDown(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        cache.clear()
        super().tearDown()

    def _create_event(self, project=None, **kwargs):
        if project is None:
            project = _make_project()
        return WebhookEvent.objects.create(
            project=project,
            payload={"type": "test", "data": {}},
            **kwargs,
        )

    # ── 成功路径 ──

    @patch("webhooks.tasks._execute_http_delivery")
    def test_deliver_success_marks_event_succeeded(self, mock_http):
        mock_http.return_value = (True, 200, {}, "ok", "", 50)
        event = self._create_event()

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.SUCCEEDED)
        self.assertIsNotNone(event.delivered_at)
        self.assertIsNone(event.delivery_locked_until)
        self.assertEqual(event.last_error, "")

    @patch("webhooks.tasks._execute_http_delivery")
    def test_deliver_success_does_not_touch_project_webhook_state(self, mock_http):
        mock_http.return_value = (True, 200, {}, "ok", "", 50)
        project = _make_project()
        event = self._create_event(project=project)

        deliver_event(event.pk)

        project.refresh_from_db()
        self.assertTrue(project.webhook_open)

    @patch("webhooks.tasks._execute_http_delivery")
    def test_deliver_success_creates_attempt(self, mock_http):
        mock_http.return_value = (True, 200, {}, "ok", "", 50)
        event = self._create_event()

        deliver_event(event.pk)

        self.assertEqual(DeliveryAttempt.objects.filter(event=event).count(), 1)
        attempt = DeliveryAttempt.objects.get(event=event)
        self.assertTrue(attempt.ok)
        self.assertEqual(attempt.try_number, 1)

    # ── 失败路径：5xx 可重试 ──

    @patch("webhooks.tasks._execute_http_delivery")
    def test_5xx_retryable_sets_schedule_locked(self, mock_http):
        mock_http.return_value = (False, 500, {}, "Internal Server Error", "", 50)
        event = self._create_event()

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.PENDING)
        self.assertIsNotNone(event.schedule_locked_until)
        self.assertGreater(event.schedule_locked_until, timezone.now())
        self.assertIsNone(event.delivery_locked_until)

    # ── 失败路径：4xx 不可重试 ──

    @patch("webhooks.tasks._execute_http_delivery")
    def test_4xx_marks_event_failed(self, mock_http):
        mock_http.return_value = (False, 404, {}, "Not Found", "", 50)
        event = self._create_event()

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.FAILED)
        self.assertIsNone(event.delivery_locked_until)

    # ── 失败路径：3xx 不可重试（修复后行为）──

    @patch("webhooks.tasks._execute_http_delivery")
    def test_3xx_marks_event_failed(self, mock_http):
        """3xx 重定向不应被视为可重试，httpx 不跟随重定向，重试无意义。"""
        mock_http.return_value = (False, 301, {}, "", "", 50)
        event = self._create_event()

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.FAILED)

    # ── 网络错误可重试 ──

    @patch("webhooks.tasks._execute_http_delivery")
    def test_network_error_retryable(self, mock_http):
        mock_http.return_value = (False, None, None, "", "ConnectError: ...", 5000)
        event = self._create_event()

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.PENDING)
        self.assertIsNotNone(event.schedule_locked_until)
        self.assertIsNone(event.delivery_locked_until)

    # ── 项目通知状态隔离 ──

    @patch("webhooks.tasks._execute_http_delivery")
    def test_retryable_failure_does_not_close_project_webhook(self, mock_http):
        """投递失败最多影响当前事件，不自动关闭项目 webhook。"""
        mock_http.return_value = (False, 500, {}, "error", "", 50)
        project = _make_project()
        event = self._create_event(project=project)

        deliver_event(event.pk)

        project.refresh_from_db()
        self.assertTrue(project.webhook_open)
        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.PENDING)
        self.assertIsNotNone(event.schedule_locked_until)

    # ── 幂等：非 PENDING 跳过 ──

    @patch("webhooks.tasks._execute_http_delivery")
    def test_skip_non_pending_event(self, mock_http):
        event = self._create_event(status=WebhookEvent.Status.SUCCEEDED)

        deliver_event(event.pk)

        mock_http.assert_not_called()

    @patch("webhooks.tasks._execute_http_delivery")
    def test_skip_event_before_retry_schedule_is_due(self, mock_http):
        # 队列中可能残留旧任务；即使任务被直接执行，也不能绕过 DB 中的下次投递时间。
        event = self._create_event(
            schedule_locked_until=timezone.now() + timezone.timedelta(minutes=5),
        )

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.PENDING)
        mock_http.assert_not_called()

    @patch("webhooks.tasks._execute_http_delivery")
    def test_skip_event_when_delivery_claim_is_still_active(self, mock_http):
        # 第一条 worker 已经抢占事件但尚未完成时，第二条 worker 不应重复通知商户。
        event = self._create_event(
            delivery_locked_until=timezone.now() + timezone.timedelta(seconds=30),
        )

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.PENDING)
        mock_http.assert_not_called()

    @patch("webhooks.tasks.deliver_event.delay")
    def test_schedule_events_skips_event_with_active_delivery_claim(self, delay_mock):
        self._create_event(
            delivery_locked_until=timezone.now() + timezone.timedelta(seconds=30),
        )

        schedule_events()

        delay_mock.assert_not_called()

    # ── webhook 未配置 ──

    @patch("webhooks.tasks._execute_http_delivery")
    def test_no_webhook_url_suspends_instead_of_failing(self, mock_http):
        """地址未配置是可恢复状态，必须挂起等待补齐而非一次判终局。

        商户改回调地址（清空保存再填新值）中间有几秒空窗，一次终局会让这期间
        确认的所有支付通知永久丢失。
        """
        project = _make_project(webhook="")
        event = self._create_event(project=project)

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.PENDING)
        self.assertIsNotNone(event.schedule_locked_until)
        self.assertIn("not configured", event.last_error)
        mock_http.assert_not_called()

    @patch("webhooks.tasks._execute_http_delivery")
    def test_no_webhook_url_fails_after_retry_budget_exhausted(self, mock_http):
        """挂起不是无限的：重试预算用尽仍未配置好，才判终局。"""
        SystemSettings.objects.create(webhook_delivery_max_retries=1)
        project = _make_project(webhook="")
        event = self._create_event(project=project)

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.FAILED)
        mock_http.assert_not_called()

    @patch("webhooks.tasks._execute_http_delivery")
    def test_private_delivery_url_is_rejected_before_http_delivery(self, mock_http):
        project = _make_project(webhook="https://127.0.0.1:8080/internal")
        event = self._create_event(project=project)

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.FAILED)
        self.assertIn("Unsafe webhook URL", event.last_error)
        mock_http.assert_not_called()

    @patch("webhooks.tasks.socket.getaddrinfo")
    @patch("webhooks.tasks._execute_http_delivery")
    def test_private_dns_delivery_url_is_rejected_before_http_delivery(
        self,
        mock_http,
        getaddrinfo_mock,
    ):
        getaddrinfo_mock.return_value = [
            (None, None, None, None, ("10.0.0.5", 443)),
        ]
        project = _make_project(webhook="https://merchant.internal.example/hook")
        event = self._create_event(project=project)

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.FAILED)
        self.assertIn("Unsafe webhook URL", event.last_error)
        mock_http.assert_not_called()

    @patch("webhooks.tasks.socket.getaddrinfo")
    @patch("webhooks.tasks._execute_http_delivery")
    def test_dns_failure_suspends_instead_of_failing(
        self,
        mock_http,
        getaddrinfo_mock,
    ):
        """DNS 瞬时解析失败必须挂起重试，不得当作 Unsafe URL 一次终局。

        解析器抖动、NS 短暂不可达都是分钟级可恢复的故障，商户域名偶发解析慢
        一次就 FAILED 会永久丢掉支付成功通知。
        """
        getaddrinfo_mock.side_effect = socket.gaierror("Name resolution failed")
        project = _make_project(webhook="https://merchant.example.com/hook")
        event = self._create_event(project=project)

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.PENDING)
        self.assertIsNotNone(event.schedule_locked_until)
        self.assertGreater(event.schedule_locked_until, timezone.now())
        self.assertIn("DNS resolution failed", event.last_error)
        mock_http.assert_not_called()

    @patch("webhooks.tasks.socket.getaddrinfo")
    @patch("webhooks.tasks._execute_http_delivery")
    def test_dns_failure_fails_after_retry_budget_exhausted(
        self,
        mock_http,
        getaddrinfo_mock,
    ):
        """挂起不是无限的：域名持续解析不出来，预算用尽照样终局。"""
        SystemSettings.objects.create(webhook_delivery_max_retries=1)
        getaddrinfo_mock.side_effect = socket.gaierror("Name resolution failed")
        project = _make_project(webhook="https://merchant.example.com/hook")
        event = self._create_event(project=project)

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.FAILED)
        mock_http.assert_not_called()

    @override_settings(WEBHOOK_ALLOW_INTERNAL_TARGETS=True)
    @patch("webhooks.tasks._execute_http_delivery")
    def test_internal_target_allowed_when_switch_on(self, mock_http):
        # 开发/压测开关开启时，http + localhost + 私有 IP 应一并放行，
        # 让 StressRun 等本地回调能完成端到端联调。
        mock_http.return_value = (True, 200, {}, "ok", "", 50)
        project = _make_project(webhook="http://localhost:8000/stress/webhook")
        event = self._create_event(project=project)

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.SUCCEEDED)
        mock_http.assert_called_once()

    # ── webhook_open=False ──

    @patch("webhooks.tasks._execute_http_delivery")
    def test_webhook_closed_suspends_instead_of_failing(self, mock_http):
        """通知开关关闭同样是可恢复状态，重新打开后事件应还能投递。"""
        project = _make_project(webhook_open=False)
        event = self._create_event(project=project)

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.PENDING)
        self.assertIsNotNone(event.schedule_locked_until)
        self.assertIn("not open", event.last_error)
        mock_http.assert_not_called()

    # ── 超出重试次数 ──

    @patch("webhooks.tasks._execute_http_delivery")
    def test_exceeds_max_retries_marks_failed(self, mock_http):
        SystemSettings.objects.create(webhook_delivery_max_retries=1)
        mock_http.return_value = (False, 500, {}, "error", "", 50)
        project = _make_project()
        event = self._create_event(project=project)
        # 模拟已有 1 次尝试
        DeliveryAttempt.objects.create(
            event=event,
            try_number=1,
            request_headers={},
            request_body="{}",
            duration_ms=50,
        )

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.FAILED)
        project.refresh_from_db()
        self.assertTrue(project.webhook_open)


class WebhookDeliveryPolicyTests(TestCase):
    """验证 GET_QUERY 与 POST_JSON 两种投递方式的分派逻辑。"""

    def tearDown(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        super().tearDown()

    @patch("webhooks.tasks._execute_http_delivery")
    def test_get_query_delivery_uses_event_delivery_url_and_success_text(
        self, mock_http
    ):
        mock_http.return_value = (True, 200, {}, "success", "", 30)
        project = _make_project(webhook="https://93.184.216.35/hook")
        event = WebhookEvent.objects.create(
            project=project,
            payload={"pid": "1001", "trade_status": "TRADE_SUCCESS"},
            delivery_url="https://93.184.216.34/notify",
            delivery_method=WebhookEvent.DeliveryMethod.GET_QUERY,
            expected_response_body="success",
        )

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.SUCCEEDED)
        call_kwargs = mock_http.call_args.kwargs
        self.assertEqual(call_kwargs["request_url"], "https://93.184.216.34/notify")
        self.assertEqual(call_kwargs["method"], "GET")
        self.assertEqual(
            call_kwargs["params"], {"pid": "1001", "trade_status": "TRADE_SUCCESS"}
        )
        self.assertEqual(call_kwargs["expected_response_body"], "success")
        # 默认未配置出口代理，请求 header 中不应出现代理转发字段
        self.assertNotIn("CF-Worker-Destination", call_kwargs["headers"])
        self.assertNotIn("CF-Worker-Key", call_kwargs["headers"])

    @patch("webhooks.tasks._egress_proxy_key", "proxy-key-secret")
    @patch("webhooks.tasks._egress_proxy_url", "https://93.184.216.36/forward")
    @patch("webhooks.tasks._execute_http_delivery")
    def test_get_query_uses_egress_proxy_when_configured(self, mock_http):
        """配置了出口代理后，GET 类型 webhook 必须走代理转发，避免暴露真实 IP / SSRF。"""
        # 任务在投递完成后会从 headers 字典中 pop 掉代理鉴权字段，避免落库；
        # 为了断言"调用 _execute_http_delivery 时刻"的 header，必须在 side_effect 里做快照。
        captured = {}

        def _capture(**kwargs):
            captured["request_url"] = kwargs["request_url"]
            captured["method"] = kwargs["method"]
            captured["params"] = kwargs["params"]
            captured["headers"] = dict(kwargs["headers"])
            return (True, 200, {}, "success", "", 30)

        mock_http.side_effect = _capture
        project = _make_project()
        event = WebhookEvent.objects.create(
            project=project,
            payload={"pid": "1001", "trade_status": "TRADE_SUCCESS"},
            delivery_url="https://93.184.216.34/notify",
            delivery_method=WebhookEvent.DeliveryMethod.GET_QUERY,
            expected_response_body="success",
        )

        deliver_event(event.pk)

        # 请求 URL 改为代理地址，原商户 URL 通过 header 传递给代理
        self.assertEqual(captured["request_url"], "https://93.184.216.36/forward")
        self.assertEqual(
            captured["headers"]["CF-Worker-Destination"],
            "https://93.184.216.34/notify",
        )
        self.assertEqual(captured["headers"]["CF-Worker-Key"], "proxy-key-secret")
        # GET 方法和 query payload 不变，签名校验交给商户端的 EPay MD5
        self.assertEqual(captured["method"], "GET")
        self.assertEqual(
            captured["params"], {"pid": "1001", "trade_status": "TRADE_SUCCESS"}
        )

    @patch("webhooks.tasks._egress_proxy_url", None)
    @patch("webhooks.tasks._execute_http_delivery")
    def test_get_query_direct_when_proxy_not_configured(self, mock_http):
        """未配置出口代理时按原状直连商户 URL。"""
        mock_http.return_value = (True, 200, {}, "success", "", 30)
        project = _make_project()
        event = WebhookEvent.objects.create(
            project=project,
            payload={"pid": "1001"},
            delivery_url="https://93.184.216.34/notify",
            delivery_method=WebhookEvent.DeliveryMethod.GET_QUERY,
            expected_response_body="success",
        )

        deliver_event(event.pk)

        call_kwargs = mock_http.call_args.kwargs
        self.assertEqual(call_kwargs["request_url"], "https://93.184.216.34/notify")
        self.assertNotIn("CF-Worker-Destination", call_kwargs["headers"])
        self.assertNotIn("CF-Worker-Key", call_kwargs["headers"])

    @patch("webhooks.tasks._egress_proxy_key", "proxy-key-secret")
    @patch("webhooks.tasks._egress_proxy_url", "https://93.184.216.36/forward")
    @patch("webhooks.tasks._execute_http_delivery")
    def test_proxy_mode_rejects_private_destination_url(self, mock_http):
        project = _make_project()
        event = WebhookEvent.objects.create(
            project=project,
            payload={"pid": "1001"},
            delivery_url="https://169.254.169.254/latest/meta-data",
            delivery_method=WebhookEvent.DeliveryMethod.GET_QUERY,
            expected_response_body="success",
        )

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.FAILED)
        self.assertIn("Unsafe webhook URL", event.last_error)
        mock_http.assert_not_called()

    @patch("webhooks.tasks._egress_proxy_key", "proxy-key-secret")
    @patch("webhooks.tasks._egress_proxy_url", "https://93.184.216.36/forward")
    @patch("webhooks.tasks._execute_http_delivery")
    def test_get_query_attempt_log_strips_proxy_credentials(self, mock_http):
        """代理鉴权 header 不能写入 DeliveryAttempt 日志，避免密钥泄漏。"""
        mock_http.return_value = (True, 200, {}, "success", "", 30)
        project = _make_project()
        event = WebhookEvent.objects.create(
            project=project,
            payload={"pid": "1001"},
            delivery_url="https://93.184.216.34/notify",
            delivery_method=WebhookEvent.DeliveryMethod.GET_QUERY,
            expected_response_body="success",
        )

        deliver_event(event.pk)

        attempt = DeliveryAttempt.objects.get(event=event)
        self.assertIsNone(attempt.request_headers.get("CF-Worker-Key"))
        self.assertIsNone(attempt.request_headers.get("CF-Worker-Destination"))

    @patch("webhooks.tasks._execute_http_delivery")
    def test_native_json_delivery_keeps_existing_ok_contract(self, mock_http):
        mock_http.return_value = (True, 200, {}, "ok", "", 30)
        project = _make_project()
        event = WebhookEvent.objects.create(
            project=project,
            payload={"type": "invoice", "data": {"sys_no": "INV-1"}},
        )

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.SUCCEEDED)
        call_kwargs = mock_http.call_args.kwargs
        self.assertEqual(call_kwargs["method"], "POST")
        self.assertEqual(call_kwargs["expected_response_body"], "ok")

    @patch("webhooks.tasks._execute_http_delivery")
    def test_get_query_works_without_project_webhook_url(self, mock_http):
        """EPay 事件使用 event.delivery_url，项目不需要配置原生 webhook。"""
        mock_http.return_value = (True, 200, {}, "success", "", 30)
        project = _make_project(webhook="")
        event = WebhookEvent.objects.create(
            project=project,
            payload={"pid": "1001"},
            delivery_url="https://93.184.216.34/notify",
            delivery_method=WebhookEvent.DeliveryMethod.GET_QUERY,
            expected_response_body="success",
        )

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.SUCCEEDED)


class WebhookResponseMatchingTests(TestCase):
    """验证 _execute_http_delivery 的响应文本匹配宽容度。

    商户的 PHP/Java 框架 echo "success" 时经常带 \\n / \\r\\n / BOM 或前后空白，
    严格相等会把这些合法响应误判为失败；strip 后精确匹配兼顾兼容性与严格度。
    """

    def _run(self, resp_text: str, expected: str = "success") -> bool:
        from webhooks.tasks import _execute_http_delivery

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.iter_bytes.return_value = iter([resp_text.encode("utf-8")])

        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__enter__.return_value = mock_response
        mock_stream_ctx.__exit__.return_value = False

        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = False
        mock_client.stream.return_value = mock_stream_ctx

        with patch("webhooks.tasks.httpx.Client", return_value=mock_client):
            ok, *_ = _execute_http_delivery(
                request_url="https://example.com",
                method="POST",
                headers={},
                body_str="{}",
                expected_response_body=expected,
            )
        return ok

    def test_exact_match_is_ok(self):
        self.assertTrue(self._run("success"))

    def test_trailing_newline_is_ok(self):
        self.assertTrue(self._run("success\n"))

    def test_crlf_is_ok(self):
        self.assertTrue(self._run("success\r\n"))

    def test_surrounding_whitespace_is_ok(self):
        self.assertTrue(self._run("  success  "))

    def test_case_mismatch_is_failure(self):
        self.assertFalse(self._run("Success"))

    def test_extra_content_is_failure(self):
        self.assertFalse(self._run("success ok"))

    def test_empty_is_failure(self):
        self.assertFalse(self._run(""))

    def test_leading_bom_is_ok(self):
        # 带 BOM 保存的 PHP 文件输出的 success 前面有 BOM，str.strip() 去不掉 BOM。
        self.assertTrue(self._run("\ufeffsuccess\r\n"))

    def test_response_truncated_to_max_bytes(self):
        """超大响应必须被截断到上限，避免恶意/异常商户回包撑爆 worker 内存。"""
        from webhooks.tasks import MAX_RESPONSE_BYTES
        from webhooks.tasks import _execute_http_delivery

        huge = b"x" * (MAX_RESPONSE_BYTES * 2)
        # 分块返回，验证迭代过程中能在到达上限时正确停止
        chunks = [huge[i : i + 8192] for i in range(0, len(huge), 8192)]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.iter_bytes.return_value = iter(chunks)

        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__enter__.return_value = mock_response
        mock_stream_ctx.__exit__.return_value = False

        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = False
        mock_client.stream.return_value = mock_stream_ctx

        with patch("webhooks.tasks.httpx.Client", return_value=mock_client):
            ok, _, _, resp_text, _, _ = _execute_http_delivery(
                request_url="https://example.com",
                method="POST",
                headers={},
                body_str="{}",
                expected_response_body="ok",
            )

        self.assertLessEqual(len(resp_text.encode("utf-8")), MAX_RESPONSE_BYTES)
        self.assertFalse(ok)


class ProjectWebhookOpenTests(TestCase):
    """项目通知开关不再承担事件重投语义。"""

    def test_reopen_webhook_does_not_reset_failed_events(self):
        project = _make_project(webhook_open=False)
        event = WebhookEvent.objects.create(
            project=project,
            payload={"type": "test"},
            status=WebhookEvent.Status.FAILED,
            schedule_locked_until=timezone.now() + timezone.timedelta(hours=1),
        )

        project.webhook_open = True
        project.save()

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.FAILED)
        self.assertIsNotNone(event.schedule_locked_until)

        project.refresh_from_db()
        self.assertTrue(project.webhook_open)


class DeliveryAttemptCountingTests(TestCase):
    """重试计数必须由认领时的原子自增承担，不能依赖 DeliveryAttempt 行数。"""

    def tearDown(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        cache.clear()
        super().tearDown()

    def test_claim_increments_attempt_count_atomically(self):
        event = WebhookEvent.objects.create(
            project=_make_project(), payload={"type": "test"}
        )

        self.assertTrue(_claim_event_for_delivery(event.pk))

        event.refresh_from_db()
        self.assertEqual(event.attempt_count, 1)

    def test_crash_before_writing_attempt_still_consumes_retry_budget(self):
        """任务在写 DeliveryAttempt 之前被杀，重试预算也必须被扣减。

        这是"永不终结黑洞"的根因回归：旧实现按 attempts.count()+1 推算次数，
        任务在写 attempt 前被杀时计数永远停在 1，max_retries 永不触发，事件
        无限重投并因 created_at 最老长期霸占调度批次头部。
        """
        SystemSettings.objects.create(webhook_delivery_max_retries=3)
        event = WebhookEvent.objects.create(
            project=_make_project(), payload={"type": "test"}
        )

        # 模拟连续三次「认领成功后进程即被杀」：没有任何 DeliveryAttempt 落库。
        for expected in (1, 2, 3):
            WebhookEvent.objects.filter(pk=event.pk).update(delivery_locked_until=None)
            self.assertTrue(_claim_event_for_delivery(event.pk))
            event.refresh_from_db()
            self.assertEqual(event.attempt_count, expected)

        self.assertEqual(event.attempts.count(), 0)
        # 计数已达上限，下一次投递失败会走终局分支而不是无限重试。
        self.assertGreaterEqual(event.attempt_count, 3)

    @patch("webhooks.tasks._execute_http_delivery")
    def test_attempt_try_number_follows_event_counter(self, mock_http):
        mock_http.return_value = (True, 200, {}, "ok", "", 50)
        event = WebhookEvent.objects.create(
            project=_make_project(), payload={"type": "test"}, attempt_count=4
        )

        deliver_event(event.pk)

        attempt = event.attempts.get()
        self.assertEqual(attempt.try_number, 5)


class RetryableStatusTests(TestCase):
    """「稍后再试」语义的 4xx 不能被判为终局。"""

    def tearDown(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        cache.clear()
        super().tearDown()

    @patch("webhooks.tasks._execute_http_delivery")
    def test_429_is_retryable(self, mock_http):
        """商户端限流期间返回 429，一次终局会让该期间的支付通知永久丢失。"""
        mock_http.return_value = (False, 429, {}, "slow down", "", 50)
        event = WebhookEvent.objects.create(
            project=_make_project(), payload={"type": "test"}
        )

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.PENDING)
        self.assertIsNotNone(event.schedule_locked_until)

    @patch("webhooks.tasks._execute_http_delivery")
    def test_408_is_retryable(self, mock_http):
        mock_http.return_value = (False, 408, {}, "timeout", "", 50)
        event = WebhookEvent.objects.create(
            project=_make_project(), payload={"type": "test"}
        )

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.PENDING)

    @patch("webhooks.tasks._execute_http_delivery")
    def test_403_remains_terminal(self, mock_http):
        """明确拒绝的 4xx 仍应终局，避免无意义重试。"""
        mock_http.return_value = (False, 403, {}, "forbidden", "", 50)
        event = WebhookEvent.objects.create(
            project=_make_project(), payload={"type": "test"}
        )

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.FAILED)

    def _create_epay_event(self):
        return WebhookEvent.objects.create(
            project=_make_project(),
            payload={"trade_status": "TRADE_SUCCESS"},
            delivery_url="https://93.184.216.34/notify",
            delivery_method=WebhookEvent.DeliveryMethod.GET_QUERY,
            expected_response_body="success",
        )

    @patch("webhooks.tasks._execute_http_delivery")
    def test_epay_200_with_unexpected_body_is_retryable(self, mock_http):
        """易支付约定商户回 success 才算收到，否则平台继续重发。"""
        mock_http.return_value = (False, 200, {}, "fail", "", 50)
        event = self._create_epay_event()

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.PENDING)
        self.assertIsNotNone(event.schedule_locked_until)

    @patch("webhooks.tasks._execute_http_delivery")
    def test_epay_200_with_unexpected_body_fails_after_retry_budget(self, mock_http):
        SystemSettings.objects.create(webhook_delivery_max_retries=1)
        mock_http.return_value = (False, 200, {}, "fail", "", 50)
        event = self._create_epay_event()

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.FAILED)

    @patch("webhooks.tasks._execute_http_delivery")
    def test_json_200_with_unexpected_body_remains_terminal(self, mock_http):
        """原生 JSON 通知 200 但响应体不符仍终局：这类商户以前只收到一次，
        改成重发可能让未做幂等的商户重复上账。"""
        mock_http.return_value = (False, 200, {}, "received", "", 50)
        event = WebhookEvent.objects.create(
            project=_make_project(), payload={"type": "test"}
        )

        deliver_event(event.pk)

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.FAILED)


class DeliveryTargetPinningTests(TestCase):
    """校验与连接必须使用同一个 IP，杜绝 DNS rebinding。"""

    @override_settings(WEBHOOK_ALLOW_INTERNAL_TARGETS=False)
    def test_safe_target_returns_validated_public_ip(self):
        self.assertEqual(
            safe_delivery_target("https://93.184.216.34/hook"), "93.184.216.34"
        )

    @override_settings(WEBHOOK_ALLOW_INTERNAL_TARGETS=False)
    def test_private_and_plain_http_targets_are_rejected(self):
        for url in (
            "https://127.0.0.1/hook",
            "https://10.0.0.5/hook",
            "https://169.254.169.254/latest/meta-data",
            "http://93.184.216.34/hook",
            "https://localhost/hook",
        ):
            with self.subTest(url=url):
                self.assertIsNone(safe_delivery_target(url))

    def test_pinned_url_keeps_hostname_for_sni(self):
        """连接目标换成 IP，但 TLS 仍须以原主机名做 SNI 与证书校验。"""
        pinned_url, extensions = pin_request_to_ip(
            "https://merchant.example.com/hook", "93.184.216.34"
        )

        self.assertEqual(pinned_url, "https://93.184.216.34/hook")
        self.assertEqual(extensions, {"sni_hostname": "merchant.example.com"})

    def test_pinned_url_preserves_port_and_path(self):
        pinned_url, extensions = pin_request_to_ip(
            "https://merchant.example.com:8443/a/b?c=d", "93.184.216.34"
        )

        self.assertEqual(pinned_url, "https://93.184.216.34:8443/a/b?c=d")
        self.assertEqual(extensions["sni_hostname"], "merchant.example.com")

    def test_ipv6_literal_is_bracketed(self):
        pinned_url, _extensions = pin_request_to_ip(
            "https://merchant.example.com/hook", "2606:2800:220:1:248:1893:25c8:1946"
        )

        self.assertEqual(
            pinned_url, "https://[2606:2800:220:1:248:1893:25c8:1946]/hook"
        )

    @patch("webhooks.tasks.socket.getaddrinfo")
    def test_rebinding_between_validation_and_connect_is_blocked(
        self, mock_getaddrinfo
    ):
        """DNS 在校验后翻转为元数据地址，连接仍会打到校验通过的那个 IP。"""
        mock_getaddrinfo.return_value = [
            (None, None, None, None, ("93.184.216.34", 443))
        ]
        pinned = safe_delivery_target("https://rebind.example.com/hook")

        # 校验之后攻击者把解析结果换成内网地址，但我们已不再解析主机名。
        mock_getaddrinfo.return_value = [
            (None, None, None, None, ("169.254.169.254", 443))
        ]
        pinned_url, _extensions = pin_request_to_ip(
            "https://rebind.example.com/hook", pinned
        )

        self.assertEqual(pinned_url, "https://93.184.216.34/hook")


class DnsResolutionTimeoutTests(SimpleTestCase):
    """DNS 解析超时必须真正生效，不能被隐式线程 join 拖回阻塞。"""

    @patch("webhooks.tasks.DNS_RESOLVE_TIMEOUT", 0.2)
    @patch("webhooks.tasks.socket.getaddrinfo")
    def test_timeout_returns_promptly_without_joining_blocked_thread(
        self, getaddrinfo_mock
    ):
        """解析线程仍阻塞时函数必须按预算及时抛出。

        曾用 `with ThreadPoolExecutor` 包超时：with 退出隐式 shutdown(wait=True)
        会 join 阻塞在 getaddrinfo 里的线程，NS 黑洞时超时形同虚设、worker 照旧
        被卡满一整批。
        """

        def blocked_resolve(*args, **kwargs):
            time.sleep(1.5)
            return [(None, None, None, None, ("93.184.216.34", 443))]

        getaddrinfo_mock.side_effect = blocked_resolve

        start = time.perf_counter()
        with self.assertRaises(DeliveryTargetResolutionError):
            resolve_addresses("blackhole.example.com", 443)
        elapsed = time.perf_counter() - start

        # 预算 0.2s、线程阻塞 1.5s：耗时接近前者才说明没有等待阻塞线程。
        self.assertLess(elapsed, 1.0)

    @patch("webhooks.tasks.socket.getaddrinfo")
    def test_resolution_error_raises_instead_of_returning_empty(self, getaddrinfo_mock):
        """解析失败抛领域异常，与「解析成功但目标不安全」严格区分。"""
        getaddrinfo_mock.side_effect = socket.gaierror("NXDOMAIN")

        with self.assertRaises(DeliveryTargetResolutionError):
            resolve_addresses("missing.example.com", 443)


class ReapStalledEventsTests(TestCase):
    """超龄未送达事件必须被强制终结，否则会持续占据调度批次头部。"""

    def tearDown(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        cache.clear()
        super().tearDown()

    def test_stalled_pending_event_is_failed(self):
        event = WebhookEvent.objects.create(
            project=_make_project(), payload={"type": "test"}
        )
        WebhookEvent.objects.filter(pk=event.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=3)
        )

        reap_stalled_events()

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.FAILED)

    def test_recent_pending_event_is_untouched(self):
        event = WebhookEvent.objects.create(
            project=_make_project(), payload={"type": "test"}
        )

        reap_stalled_events()

        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.PENDING)


class WebhookDeliveryClientIsolationTests(SimpleTestCase):
    """商户投递固定到已校验 IP，连接池按 IP 复用会让同 IP 的商户共用 TLS 连接。"""

    def test_delivery_uses_isolated_client_with_shared_ssl_context(self):
        from common import http_client
        from webhooks.tasks import _execute_http_delivery

        mock_response = MagicMock(status_code=200, headers={})
        mock_response.iter_bytes.return_value = iter([b"ok"])
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.stream.return_value.__enter__.return_value = mock_response

        with (
            patch(
                "webhooks.tasks.httpx.Client", return_value=mock_client
            ) as client_cls,
            patch.object(http_client, "shared_client") as shared_client_mock,
        ):
            ok, *_ = _execute_http_delivery(
                request_url="https://merchant.example/notify",
                headers={},
                body_str="{}",
                pinned_ip="93.184.216.34",
            )

        self.assertTrue(ok)
        shared_client_mock.assert_not_called()
        client_cls.assert_called_once()
        self.assertIs(client_cls.call_args.kwargs["verify"], http_client.ssl_context())
        # 证书仍按原主机名校验：SNI 走 extensions，连接目标改成已校验 IP。
        stream_kwargs = mock_client.stream.call_args.kwargs
        self.assertEqual(
            stream_kwargs["extensions"], {"sni_hostname": "merchant.example"}
        )
