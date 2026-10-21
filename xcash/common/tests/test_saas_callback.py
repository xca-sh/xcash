from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.test import override_settings


class SaasCallbackTest(TestCase):
    @override_settings(
        IS_SAAS=True,
        SAAS_API_TOKEN="test-token",
        SAAS_CALLBACK_URL="http://saas.local",
    )
    @patch("common.saas_callback.http_client")
    def test_deliver_sends_post_with_bearer_token(self, mock_http):
        from common.saas_callback import deliver_saas_callback

        mock_client = mock_http

        deliver_saas_callback(
            payload={
                "event": "invoice.confirmed",
                "appid": "XC-test",
                "sys_no": "INV-001",
                "worth": "100.00",
                "currency": "USDT",
            }
        )

        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.args[0] == "http://saas.local/callbacks/xcash"
        assert "Authorization" in call_kwargs.kwargs["headers"]
        payload = call_kwargs.kwargs["json"]
        assert payload["event"] == "invoice.confirmed"
        assert payload["appid"] == "XC-test"
        assert payload["sys_no"] == "INV-001"
        assert payload["worth"] == "100.00"

    @override_settings(IS_SAAS=False)
    @patch("common.saas_callback.http_client")
    def test_deliver_skips_when_token_missing(self, mock_http):
        from common.saas_callback import deliver_saas_callback

        deliver_saas_callback(
            payload={
                "event": "invoice.confirmed",
                "appid": "XC-test",
                "sys_no": "INV-001",
                "worth": "100.00",
                "currency": "USDT",
            }
        )

        mock_http.post.assert_not_called()

    @override_settings(
        IS_SAAS=True,
        SAAS_API_TOKEN="test-token",
        SAAS_CALLBACK_URL="http://saas.local",
    )
    @patch("common.saas_callback.http_client")
    def test_deliver_retries_on_http_error(self, mock_http):
        import httpx

        from common.saas_callback import deliver_saas_callback

        mock_client = mock_http
        mock_response = httpx.Response(
            status_code=500, request=httpx.Request("POST", "http://test")
        )
        mock_client.post.return_value = mock_response

        mock_client.post.return_value.raise_for_status = lambda: (_ for _ in ()).throw(
            httpx.HTTPStatusError(
                "Server Error", request=mock_response.request, response=mock_response
            )
        )

        with self.assertRaises(httpx.HTTPStatusError):
            deliver_saas_callback(
                payload={
                    "event": "invoice.confirmed",
                    "appid": "XC-test",
                    "sys_no": "INV-001",
                    "worth": "100.00",
                    "currency": "USDT",
                }
            )

    def test_to_payload_emits_type_specific_amount_field(self):
        from common.saas_callback import CallbackEvent
        from common.saas_callback import SaasCallback

        # invoice/deposit：带 worth，不带 tx_detail
        worth_payload = SaasCallback(
            event=CallbackEvent.INVOICE_CONFIRMED,
            appid="XC-test",
            sys_no="INV-001",
            currency="USDT",
            worth="100.00",
        ).to_payload()
        assert worth_payload["event"] == "invoice.confirmed"  # 序列化为纯字符串
        assert worth_payload["worth"] == "100.00"
        assert "tx_detail" not in worth_payload
        assert "timestamp" in worth_payload

        # gas_fee：带 tx_detail，不带 worth
        gas_payload = SaasCallback(
            event=CallbackEvent.GAS_FEE_VAULT_SLOT_DEPLOY,
            appid="XC-test",
            sys_no="vault-slot-deploy:1",
            currency="USDT",
            uid="42",
            tx_detail={"gas_cost": "0.042"},
        ).to_payload()
        assert gas_payload["event"] == "gas_fee.vault_slot_deploy.confirmed"
        assert gas_payload["uid"] == "42"
        assert gas_payload["tx_detail"] == {"gas_cost": "0.042"}
        assert "worth" not in gas_payload
        assert "uid" not in worth_payload

        # aml：只报一次查询，不带任何金额字段
        aml_payload = SaasCallback(
            event=CallbackEvent.AML_SCREENED,
            appid="XC-test",
            sys_no="aml-screening:INV-001",
        ).to_payload()
        assert aml_payload["event"] == "aml.screened"
        assert aml_payload["sys_no"] == "aml-screening:INV-001"
        assert not {"worth", "tx_detail", "currency"} & aml_payload.keys()

        # vault_slot：只带 detail（地址 / 链 / 用途），不带金额
        slot_payload = SaasCallback(
            event=CallbackEvent.VAULT_SLOT_CREATED,
            appid="XC-test",
            sys_no="vault-slot-open:0xabc",
            detail={"address": "0xabc", "chain": "ethereum", "usage": "deposit"},
        ).to_payload()
        assert slot_payload["event"] == "vault_slot.created"
        assert slot_payload["detail"]["usage"] == "deposit"
        assert not {"worth", "tx_detail"} & slot_payload.keys()
        assert "detail" not in aml_payload

    def test_invalid_event_value_is_rejected(self):
        import pytest

        from common.saas_callback import SaasCallback

        with pytest.raises(ValueError, match=r"invoice\.paid"):
            SaasCallback(
                event="invoice.paid",  # 不在 CallbackEvent 内
                appid="XC-test",
                sys_no="INV-001",
                currency="USDT",
                worth="100.00",
            )

    def test_amount_field_must_match_event_family(self):
        import pytest

        from common.saas_callback import CallbackEvent
        from common.saas_callback import SaasCallback

        # invoice/deposit 缺 worth → 报错
        with pytest.raises(ValueError, match="必须且只能带 worth"):
            SaasCallback(
                event=CallbackEvent.INVOICE_CONFIRMED,
                appid="XC-test",
                sys_no="INV-001",
                currency="USDT",
            )
        # gas_fee 缺 tx_detail（误用 worth）→ 报错
        with pytest.raises(ValueError, match="gas_fee 回调必须且只能带 tx_detail"):
            SaasCallback(
                event=CallbackEvent.GAS_FEE_VAULT_SLOT_DEPLOY,
                appid="XC-test",
                sys_no="vault-slot-deploy:1",
                currency="USDT",
                worth="0.042",
            )
        # aml 误带金额 → 报错（单价只由 SaaS 套餐决定）
        with pytest.raises(ValueError, match="aml 回调不带 worth / tx_detail"):
            SaasCallback(
                event=CallbackEvent.AML_SCREENED,
                appid="XC-test",
                sys_no="aml-screening:INV-001",
                worth="1",
            )

    def test_detail_only_allowed_for_vault_slot_family(self):
        import pytest

        from common.saas_callback import CallbackEvent
        from common.saas_callback import SaasCallback

        with pytest.raises(ValueError, match="vault_slot 回调必须且只能带 detail"):
            SaasCallback(
                event=CallbackEvent.VAULT_SLOT_CREATED,
                appid="XC-test",
                sys_no="vault-slot-open:0xabc",
            )
        with pytest.raises(ValueError, match="vault_slot 回调必须且只能带 detail"):
            SaasCallback(
                event=CallbackEvent.VAULT_SLOT_CREATED,
                appid="XC-test",
                sys_no="vault-slot-open:0xabc",
                detail={"address": "0xabc"},
                worth="0.01",
            )
        with pytest.raises(ValueError, match="不带 detail"):
            SaasCallback(
                event=CallbackEvent.AML_SCREENED,
                appid="XC-test",
                sys_no="aml-screening:INV-001",
                detail={"address": "0xabc"},
            )

    def test_http_errors_are_retried_by_table_until_exhausted(self):
        import httpx

        from common.saas_callback import SAAS_CALLBACK_RETRY_DELAYS
        from common.saas_callback import deliver_saas_callback

        strategy = deliver_saas_callback.retry_strategy
        error = httpx.ConnectError("saas down")

        for attempts in range(len(SAAS_CALLBACK_RETRY_DELAYS)):
            job = SimpleNamespace(attempts=attempts)
            assert strategy.get_retry_decision(exception=error, job=job) is not None
        exhausted = SimpleNamespace(attempts=len(SAAS_CALLBACK_RETRY_DELAYS))
        assert strategy.get_retry_decision(exception=error, job=exhausted) is None
        # 非 HTTP 错误（代码缺陷）不重试，直接落为 failed 供排查。
        job = SimpleNamespace(attempts=0)
        assert strategy.get_retry_decision(exception=KeyError("x"), job=job) is None

    @override_settings(
        IS_SAAS=True,
        DEBUG=True,
        SAAS_API_TOKEN="test-token",
        SAAS_CALLBACK_URL="http://saas.local",
    )
    @patch("common.saas_callback.http_client")
    def test_debug_http_failure_is_not_retried(self, mock_http):
        import httpx

        from common.saas_callback import deliver_saas_callback

        mock_http.post.side_effect = httpx.ConnectError("saas down")

        # DEBUG 下只通知一次：不抛出，任务不会进入重试。
        deliver_saas_callback(payload={"event": "invoice.confirmed"})

    @override_settings(IS_SAAS=True)
    def test_callback_is_enqueued_with_business_transaction(self):
        from django.db import transaction

        from common.jobs import PRIORITY_URGENT
        from common.saas_callback import CallbackEvent
        from common.saas_callback import SaasCallback
        from common.saas_callback import send_saas_callback
        from common.tests.job_helpers import queued_jobs

        def callback(sys_no):
            return SaasCallback(
                event=CallbackEvent.INVOICE_CONFIRMED,
                appid="XC-test",
                sys_no=sys_no,
                worth="1",
            )

        # 业务确认事务回滚：回调随之消失，不会出现「SaaS 已计费、账上没确认」。
        with self.assertRaises(RuntimeError), transaction.atomic():
            send_saas_callback(callback("INV-ROLLBACK"))
            raise RuntimeError("rollback")
        assert queued_jobs("common.deliver_saas_callback") == []

        with transaction.atomic():
            send_saas_callback(callback("INV-COMMIT"))

        [job] = queued_jobs("common.deliver_saas_callback")
        assert job.args["payload"]["sys_no"] == "INV-COMMIT"
        assert job.priority == PRIORITY_URGENT

    @override_settings(IS_SAAS=False)
    def test_self_hosted_does_not_enqueue_callback(self):
        from common.saas_callback import CallbackEvent
        from common.saas_callback import SaasCallback
        from common.saas_callback import send_saas_callback
        from common.tests.job_helpers import queued_jobs

        send_saas_callback(
            SaasCallback(
                event=CallbackEvent.INVOICE_CONFIRMED,
                appid="XC-test",
                sys_no="INV-1",
                worth="1",
            )
        )

        assert queued_jobs("common.deliver_saas_callback") == []
