from unittest.mock import patch

from django.test import TestCase
from django.test import override_settings


class SaasCallbackTest(TestCase):
    @override_settings(
        IS_SAAS=True,
        SAAS_API_TOKEN="test-token",
        SAAS_CALLBACK_URL="http://saas.local",
    )
    @patch("common.saas_callback.httpx.Client")
    def test_deliver_sends_post_with_bearer_token(self, mock_client_cls):
        from common.saas_callback import _deliver_saas_callback

        mock_client = mock_client_cls.return_value.__enter__.return_value

        _deliver_saas_callback(
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
    @patch("common.saas_callback.httpx.Client")
    def test_deliver_skips_when_token_missing(self, mock_client_cls):
        from common.saas_callback import _deliver_saas_callback

        _deliver_saas_callback(
            payload={
                "event": "invoice.confirmed",
                "appid": "XC-test",
                "sys_no": "INV-001",
                "worth": "100.00",
                "currency": "USDT",
            }
        )

        mock_client_cls.assert_not_called()

    @override_settings(
        IS_SAAS=True,
        SAAS_API_TOKEN="test-token",
        SAAS_CALLBACK_URL="http://saas.local",
    )
    @patch("common.saas_callback.httpx.Client")
    def test_deliver_retries_on_http_error(self, mock_client_cls):
        import httpx

        from common.saas_callback import _deliver_saas_callback

        mock_client = mock_client_cls.return_value.__enter__.return_value
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
            _deliver_saas_callback(
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
            tx_detail={"gas_cost": "0.042"},
        ).to_payload()
        assert gas_payload["event"] == "gas_fee.vault_slot_deploy.confirmed"
        assert gas_payload["tx_detail"] == {"gas_cost": "0.042"}
        assert "worth" not in gas_payload

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

    def test_retry_countdown_is_monotonic_and_capped(self):
        from common.saas_callback import _retry_countdown

        retry_delays = [_retry_countdown(retries) for retries in range(6)]
        assert retry_delays == sorted(retry_delays)
        assert _retry_countdown(6) == retry_delays[-1]
        assert _retry_countdown(100) == retry_delays[-1]
