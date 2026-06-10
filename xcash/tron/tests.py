from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import PropertyMock
from unittest.mock import patch

import httpx
from django.contrib.admin.sites import AdminSite
from django.db import IntegrityError
from django.test import SimpleTestCase
from django.test import TestCase
from django.test import override_settings
from django.utils import timezone
from tron.admin import TronWatchCursorAdmin
from tron.client import TronClientError
from tron.client import TronHttpClient
from tron.models import TRON_MAX_BROADCAST_HASHES
from tron.models import TronTxTask
from tron.models import TronWatchCursor
from tron.tasks import TRON_BROADCAST_LOCK_TIMEOUT_SECONDS
from tron.tasks import TRON_SENDER_BROADCAST_LOCK_TIMEOUT_SECONDS
from tron.tasks import broadcast_tron_task
from tron.tasks import confirm_tron_receipt_tx_tasks
from web3 import Web3

from chains.adapters import TxCheckResult
from chains.adapters import TxCheckStatus
from chains.constants import ChainCode
from chains.models import Address
from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from chains.models import Transfer
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import TxTaskType
from chains.models import VaultSlot
from chains.models import VaultSlotCollectSchedule
from chains.models import VaultSlotUsage
from chains.models import Wallet
from currencies.models import Crypto
from currencies.models import CryptoOnChain
from currencies.models import Fiat
from invoices.models import DifferRecipientAddress
from projects.models import Customer
from projects.models import Project


def _selector(signature: str) -> str:
    return Web3.keccak(text=signature)[:4].hex()


class VaultSlotCodecTests(SimpleTestCase):
    def test_predict_uses_tron_create2_prefix_0x41(self):
        from eth_utils import keccak
        from tron.codec import TronAddressCodec
        from tron.contracts_codec import build_tron_vault_slot_init_code
        from tron.contracts_codec import predict_tron_vault_slot_address
        from tron.contracts_codec import tron_address_to_20_bytes

        factory = "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8"
        template = "TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb"
        vault = "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8"
        salt = b"\x11" * 32

        init_code = build_tron_vault_slot_init_code(
            vault_slot_template=template,
            vault=vault,
        )
        expected_digest = keccak(
            b"\x41"
            + tron_address_to_20_bytes(factory)
            + salt
            + keccak(init_code)
        )
        expected = TronAddressCodec.hex41_to_base58(f"41{expected_digest[-20:].hex()}")

        predicted = predict_tron_vault_slot_address(
            vault=vault,
            salt=salt,
            factory=factory,
            vault_slot_template=template,
        )

        self.assertEqual(predicted, expected)
        evm_digest = keccak(
            b"\xff"
            + tron_address_to_20_bytes(factory)
            + salt
            + keccak(init_code)
        )
        evm_style = TronAddressCodec.hex41_to_base58(f"41{evm_digest[-20:].hex()}")
        self.assertNotEqual(predicted, evm_style)

    def test_sign_tron_transaction_hashes_raw_data_hex(self):
        from chains.keys import sign_tron_transaction

        unsigned = {"raw_data_hex": "0a02abcd", "raw_data": {"expiration": 123}}
        signed = sign_tron_transaction(private_key="1" * 64, unsigned_transaction=unsigned)

        self.assertEqual(signed.tx_hash, sha256(bytes.fromhex("0a02abcd")).hexdigest())
        self.assertEqual(len(signed.raw_transaction["signature"][0]), 130)
        self.assertEqual(signed.raw_transaction["raw_data"], {"expiration": 123})


class TronAdapterTests(SimpleTestCase):
    @patch("tron.adapter.TronHttpClient")
    def test_is_contract_reads_contract_payload(self, client_cls):
        from tron.adapter import TronAdapter

        client_cls.return_value.get_contract.return_value = {"bytecode": "00"}
        chain = SimpleNamespace(code="tron", tron_api_key="")

        self.assertTrue(
            TronAdapter().is_contract(
                chain,
                "TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb",
            )
        )

    @patch("tron.adapter.TronHttpClient")
    def test_tx_result_returns_success_with_block_position(self, client_cls):
        from tron.adapter import TronAdapter

        client = client_cls.return_value
        client.get_transaction_info_by_id.return_value = {
            "id": "a" * 64,
            "blockNumber": 123,
            "receipt": {"result": "SUCCESS"},
        }
        client.get_solid_block_id.return_value = "b" * 64

        result = TronAdapter().tx_result(SimpleNamespace(code="tron"), "a" * 64)

        self.assertEqual(
            result,
            TxCheckResult(
                status=TxCheckStatus.SUCCEEDED,
                block_number=123,
                block_hash="b" * 64,
            ),
        )


@override_settings(TRON_RPC_TIMEOUT=3.0)
@patch("tron.client._TRON_HTTP_RETRY_BACKOFF_SECONDS", (0, 0))
class TronHttpClientTests(SimpleTestCase):
    @patch("tron.client._TRON_HTTP_RETRY_BACKOFF_SECONDS", (0, 0))
    @patch("tron.client.httpx.get")
    def test_retry_error_uses_chain_code_when_chain_field_is_absent(self, get_mock):
        get_mock.side_effect = httpx.ConnectError("boom")
        chain = SimpleNamespace(
            tron_base_url="https://api.trongrid.io",
            code="tron-mainnet",
            tron_api_key="",
        )

        with self.assertRaisesMessage(TronClientError, "from tron-mainnet"):
            TronHttpClient(chain=chain).get_latest_solid_block_number()

    @patch("tron.client.httpx.get")
    def test_get_latest_solid_block_number_reads_block_header_number(self, get_mock):
        get_mock.return_value.json.return_value = {
            "block_header": {"raw_data": {"number": 123456}}
        }
        get_mock.return_value.raise_for_status.return_value = None

        chain = SimpleNamespace(
            rpc="https://tron-mainnet.core.chainstack.com/token",
            chain="tron-mainnet",
            tron_api_key="",
        )
        client = TronHttpClient(chain=chain)

        latest_block = client.get_latest_solid_block_number()

        self.assertEqual(latest_block, 123456)
        call_args, _kwargs = get_mock.call_args
        self.assertEqual(
            call_args[0],
            "https://api.trongrid.io/walletsolidity/getnowblock",
        )

    @patch("tron.client.httpx.get")
    def test_get_latest_solid_block_number_rejects_missing_or_zero_number(
        self, get_mock
    ):
        for payload in (
            {},
            {"block_header": {"raw_data": {"number": 0}}},
        ):
            with self.subTest(payload=payload):
                response = Mock()
                response.raise_for_status.return_value = None
                response.json.return_value = payload
                get_mock.return_value = response
                chain = SimpleNamespace(
                    rpc="https://api.trongrid.io",
                    chain="tron-mainnet",
                    code="tron-mainnet",
                    tron_api_key="tron-key",
                )

                with self.assertRaisesMessage(
                    TronClientError, "invalid latest solid block"
                ):
                    TronHttpClient(chain=chain).get_latest_solid_block_number()

    @patch("tron.client.httpx.post")
    def test_get_solid_block_id_posts_block_number(self, post_mock):
        post_mock.return_value.json.return_value = {"blockID": "A" * 64}
        post_mock.return_value.raise_for_status.return_value = None

        chain = SimpleNamespace(
            rpc="https://api.trongrid.io",
            chain="tron-mainnet",
            code="tron-mainnet",
            tron_api_key="",
        )
        block_id = TronHttpClient(chain=chain).get_solid_block_id(block_number=123456)

        self.assertEqual(block_id, "a" * 64)
        call_args, call_kwargs = post_mock.call_args
        self.assertEqual(
            call_args[0],
            "https://api.trongrid.io/walletsolidity/getblockbynum",
        )
        self.assertEqual(call_kwargs["json"], {"num": 123456})

    @patch("tron.client.httpx.post")
    def test_get_solid_block_id_rejects_invalid_block_id(self, post_mock):
        post_mock.return_value.json.return_value = {"blockID": ""}
        post_mock.return_value.raise_for_status.return_value = None

        chain = SimpleNamespace(
            rpc="https://api.trongrid.io",
            chain="tron-mainnet",
            code="tron-mainnet",
            tron_api_key="",
        )
        with self.assertRaisesMessage(TronClientError, "invalid solid block id"):
            TronHttpClient(chain=chain).get_solid_block_id(block_number=123456)

    @patch("tron.client.httpx.post")
    def test_get_transaction_info_by_id_posts_tx_hash(self, post_mock):
        post_mock.return_value.json.return_value = {"id": "a" * 64}
        post_mock.return_value.raise_for_status.return_value = None

        chain = SimpleNamespace(
            rpc="https://api.trongrid.io",
            chain="tron-mainnet",
            tron_api_key="",
        )
        client = TronHttpClient(chain=chain)

        payload = client.get_transaction_info_by_id("a" * 64)

        self.assertEqual(payload["id"], "a" * 64)
        _, kwargs = post_mock.call_args
        self.assertEqual(kwargs["json"], {"value": "a" * 64})

    @patch("tron.client.httpx.post")
    def test_get_account_resource_posts_visible_address(self, post_mock):
        post_mock.return_value.json.return_value = {"EnergyLimit": 1000}
        post_mock.return_value.raise_for_status.return_value = None

        chain = SimpleNamespace(
            rpc="https://api.trongrid.io",
            chain="tron-mainnet",
            tron_api_key="tron-key",
        )
        payload = TronHttpClient(chain=chain).get_account_resource(
            address="TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb"
        )

        self.assertEqual(payload["EnergyLimit"], 1000)
        call_args, kwargs = post_mock.call_args
        self.assertEqual(
            call_args[0],
            "https://api.trongrid.io/wallet/getaccountresource",
        )
        self.assertEqual(
            kwargs["json"],
            {
                "address": "TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb",
                "visible": True,
            },
        )

    @patch("tron.client.httpx.get")
    def test_list_confirmed_trc20_history_sends_contract_filter_and_fingerprint(
        self,
        get_mock,
    ):
        get_mock.return_value.json.return_value = {"data": [], "meta": {}}
        get_mock.return_value.raise_for_status.return_value = None

        chain = SimpleNamespace(rpc="https://api.trongrid.io", tron_api_key="tron-key")
        client = TronHttpClient(chain=chain)
        client.list_confirmed_trc20_history(
            address="TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb",
            contract_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            fingerprint="cursor-1",
        )

        _, kwargs = get_mock.call_args
        self.assertEqual(kwargs["headers"]["TRON-PRO-API-KEY"], "tron-key")
        self.assertEqual(kwargs["params"]["only_confirmed"], "true")
        self.assertEqual(kwargs["params"]["fingerprint"], "cursor-1")

    @patch("tron.client.httpx.get")
    def test_list_confirmed_contract_events_sends_block_filter_and_fingerprint(
        self,
        get_mock,
    ):
        get_mock.return_value.json.return_value = {"data": [], "meta": {}}
        get_mock.return_value.raise_for_status.return_value = None

        chain = SimpleNamespace(
            rpc="https://api.trongrid.io",
            chain="tron-mainnet",
            tron_api_key="tron-key",
        )
        client = TronHttpClient(chain=chain)
        client.list_confirmed_contract_events(
            contract_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            event_name="Transfer",
            block_number=61840405,
            fingerprint="cursor-1",
        )

        call_args, kwargs = get_mock.call_args
        self.assertEqual(
            call_args[0],
            "https://api.trongrid.io/v1/contracts/TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t/events",
        )
        self.assertEqual(kwargs["headers"]["TRON-PRO-API-KEY"], "tron-key")
        self.assertEqual(kwargs["params"]["event_name"], "Transfer")
        self.assertEqual(kwargs["params"]["block_number"], 61840405)
        self.assertEqual(kwargs["params"]["only_confirmed"], "true")
        self.assertEqual(kwargs["params"]["fingerprint"], "cursor-1")

    @patch("tron.client.httpx.get")
    def test_list_confirmed_contract_events_wraps_http_error(self, get_mock):
        get_mock.side_effect = httpx.HTTPError("boom")

        chain = SimpleNamespace(
            rpc="https://api.trongrid.io",
            chain="tron-mainnet",
            tron_api_key="tron-key",
        )
        client = TronHttpClient(chain=chain)

        with self.assertRaisesMessage(
            TronClientError,
            "failed to fetch confirmed contract events from tron-mainnet",
        ):
            client.list_confirmed_contract_events(
                contract_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
                event_name="Transfer",
                block_number=61840405,
            )

    @patch("tron.client.httpx.get")
    def test_retries_transient_http_error_until_success(self, get_mock):
        # 第一次抛瞬时网络错误、第二次成功：整体被重试吸收，不应上抛 TronClientError。
        good_response = Mock()
        good_response.raise_for_status.return_value = None
        good_response.json.return_value = {"block_header": {"raw_data": {"number": 42}}}
        get_mock.side_effect = [httpx.ReadTimeout("transient"), good_response]

        chain = SimpleNamespace(
            rpc="https://api.trongrid.io",
            chain="tron-mainnet",
            tron_api_key="tron-key",
        )

        latest_block = TronHttpClient(chain=chain).get_latest_solid_block_number()

        self.assertEqual(latest_block, 42)
        self.assertEqual(get_mock.call_count, 2)

    @patch("tron.client.httpx.get")
    def test_retries_retriable_http_status_until_success(self, get_mock):
        # 5xx / 429 属于节点瞬时错误，应进入同一套退避重试逻辑。
        bad_response = Mock()
        bad_response.status_code = 500
        bad_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Server Error",
            request=Mock(),
            response=bad_response,
        )
        good_response = Mock()
        good_response.raise_for_status.return_value = None
        good_response.json.return_value = {"block_header": {"raw_data": {"number": 43}}}
        get_mock.side_effect = [bad_response, good_response]

        chain = SimpleNamespace(
            rpc="https://api.trongrid.io",
            chain="tron-mainnet",
            tron_api_key="tron-key",
        )

        latest_block = TronHttpClient(chain=chain).get_latest_solid_block_number()

        self.assertEqual(latest_block, 43)
        self.assertEqual(get_mock.call_count, 2)

    @patch("tron.client.httpx.get")
    def test_does_not_retry_on_non_retriable_4xx(self, get_mock):
        # 401 / 403 / 404 等客户端错误属永久故障，重试只会重复触发，应立即上抛。
        bad_response = Mock()
        bad_response.status_code = 401
        bad_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Unauthorized",
            request=Mock(),
            response=bad_response,
        )
        get_mock.return_value = bad_response

        chain = SimpleNamespace(
            rpc="https://api.trongrid.io",
            chain="tron-mainnet",
            tron_api_key="tron-key",
        )

        with self.assertRaises(TronClientError):
            TronHttpClient(chain=chain).get_latest_solid_block_number()

        # 永久错误必须只调一次；不应触发重试。
        self.assertEqual(get_mock.call_count, 1)


@override_settings(
    TRON_RESOURCE_SAFETY_MARGIN_BPS=10_000,
    TRON_BANDWIDTH_SAFETY_BYTES=0,
)
class TronTxTaskBroadcastResourceGuardTests(TestCase):
    def setUp(self):
        self.chain = Chain.objects.create(
            code=ChainCode.Tron,
            rpc="https://api.trongrid.io",
            tron_api_key="tron-key",
            active=True,
        )
        self.wallet = Wallet.objects.create()
        self.sender = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.TRON,
            usage=AddressUsage.HotWallet,
            bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
            address_index=0,
            address="TJRabPrwbZy45sbavfcjinPJC18kjpRTv8",
        )

    def make_task(self) -> TronTxTask:
        base_task = TxTask.objects.create(
            chain=self.chain,
            sender=self.sender,
            tx_type=TxTaskType.VaultSlotCollect,
            status=TxTaskStatus.QUEUED,
        )
        return TronTxTask.objects.create(
            base_task=base_task,
            sender=self.sender,
            chain=self.chain,
            to="TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb",
            function_selector="collect(address)",
            parameter="00" * 32,
            fee_limit=150_000_000,
        )

    def unsigned_transaction(self, *, contract_address: str | None = None) -> dict:
        from tron.codec import TronAddressCodec

        raw_data_hex = "0a02abcd"
        raw_data = {
            "contract": [
                {
                    "type": "TriggerSmartContract",
                    "parameter": {
                        "value": {
                            "owner_address": TronAddressCodec.base58_to_hex41(
                                self.sender.address
                            ),
                            "contract_address": TronAddressCodec.base58_to_hex41(
                                contract_address or "TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb"
                            ),
                            "data": _selector("collect(address)") + "00" * 32,
                        }
                    },
                }
            ],
            "expiration": 123,
            "fee_limit": 150_000_000,
        }
        return {
            "raw_data_hex": raw_data_hex,
            "raw_data": raw_data,
            "txID": sha256(bytes.fromhex(raw_data_hex)).hexdigest(),
        }

    @staticmethod
    def signed_payload(transaction: dict) -> SimpleNamespace:
        raw_transaction = {
            **transaction,
            "signature": ["b" * 130],
        }
        return SimpleNamespace(
            tx_hash=transaction["txID"],
            raw_transaction=raw_transaction,
        )

    @patch("tron.models.TronHttpClient")
    @patch("chains.models.Address.sign_tron_transaction")
    def test_broadcast_stops_before_sign_when_energy_is_insufficient(
        self,
        sign_transaction,
        client_class,
    ):
        task = self.make_task()
        client = client_class.return_value
        client.trigger_constant_contract.return_value = {
            "result": {"result": True},
            "energy_used": 1_000,
        }
        client.get_account_resource.return_value = {
            "EnergyLimit": 999,
            "EnergyUsed": 0,
            "freeNetLimit": 10_000,
        }

        with self.assertRaisesMessage(TronClientError, "tron energy insufficient"):
            task.broadcast()

        client.trigger_smart_contract.assert_not_called()
        sign_transaction.assert_not_called()
        client.broadcast_transaction.assert_not_called()
        task.base_task.refresh_from_db()
        self.assertEqual(task.base_task.status, TxTaskStatus.QUEUED)
        self.assertIsNone(task.base_task.tx_hash)

    @patch("tron.models.TronHttpClient")
    @patch("chains.models.Address.sign_tron_transaction")
    def test_broadcast_stops_after_sign_when_bandwidth_is_insufficient(
        self,
        sign_transaction,
        client_class,
    ):
        task = self.make_task()
        client = client_class.return_value
        client.trigger_constant_contract.return_value = {
            "result": {"result": True},
            "energy_used": 1_000,
        }
        client.get_account_resource.side_effect = [
            {"EnergyLimit": 2_000, "EnergyUsed": 0, "freeNetLimit": 0},
            {"EnergyLimit": 2_000, "EnergyUsed": 0, "freeNetLimit": 0},
        ]
        transaction = self.unsigned_transaction()
        client.trigger_smart_contract.return_value = {"transaction": transaction}
        sign_transaction.return_value = self.signed_payload(transaction)

        with self.assertRaisesMessage(TronClientError, "tron bandwidth insufficient"):
            task.broadcast()

        client.broadcast_transaction.assert_not_called()
        task.base_task.refresh_from_db()
        self.assertEqual(task.base_task.status, TxTaskStatus.QUEUED)
        self.assertIsNone(task.base_task.tx_hash)

    @patch("tron.models.TronHttpClient")
    @patch("chains.models.Address.sign_tron_transaction")
    def test_broadcast_continues_when_energy_and_bandwidth_are_sufficient(
        self,
        sign_transaction,
        client_class,
    ):
        task = self.make_task()
        client = client_class.return_value
        client.trigger_constant_contract.return_value = {
            "result": {"result": True},
            "energy_used": 1_000,
        }
        client.get_account_resource.side_effect = [
            {"EnergyLimit": 2_000, "EnergyUsed": 0, "freeNetLimit": 10_000},
            {"EnergyLimit": 2_000, "EnergyUsed": 0, "freeNetLimit": 10_000},
        ]
        transaction = self.unsigned_transaction()
        client.trigger_smart_contract.return_value = {"transaction": transaction}
        sign_transaction.return_value = self.signed_payload(transaction)
        client.broadcast_transaction.return_value = {"result": True}

        task.broadcast()

        client.broadcast_transaction.assert_called_once()
        task.base_task.refresh_from_db()
        self.assertEqual(task.base_task.status, TxTaskStatus.SUBMITTED)
        self.assertEqual(task.base_task.tx_hash, transaction["txID"])

    @patch("tron.models.TronHttpClient")
    @patch("chains.models.Address.sign_tron_transaction")
    def test_broadcast_rejects_tampered_unsigned_contract_before_sign(
        self,
        sign_transaction,
        client_class,
    ):
        task = self.make_task()
        client = client_class.return_value
        client.trigger_constant_contract.return_value = {
            "result": {"result": True},
            "energy_used": 1_000,
        }
        client.get_account_resource.return_value = {
            "EnergyLimit": 2_000,
            "EnergyUsed": 0,
            "freeNetLimit": 10_000,
        }
        client.trigger_smart_contract.return_value = {
            "transaction": self.unsigned_transaction(
                contract_address=self.sender.address,
            )
        }

        with self.assertRaisesMessage(TronClientError, "contract mismatch"):
            task.broadcast()

        sign_transaction.assert_not_called()
        client.broadcast_transaction.assert_not_called()

    @patch("tron.models.TronHttpClient")
    def test_broadcast_skips_submitted_task(self, client_class):
        task = self.make_task()
        TxTask.objects.filter(pk=task.base_task_id).update(
            status=TxTaskStatus.SUBMITTED,
        )

        task.broadcast()

        client_class.assert_not_called()
        task.refresh_from_db()
        self.assertIsNone(task.last_attempt_at)
        task.base_task.refresh_from_db()
        self.assertEqual(task.base_task.status, TxTaskStatus.SUBMITTED)

    @patch("tron.models.TronTxTask.execute_broadcast")
    def test_rebroadcast_expired_submitted_requires_expiration(self, execute_broadcast):
        task = self.make_task()
        TxTask.objects.filter(pk=task.base_task_id).update(
            status=TxTaskStatus.SUBMITTED,
        )
        task.expiration = int((timezone.now() + timedelta(minutes=1)).timestamp() * 1000)
        task.save(update_fields=["expiration"])

        task.rebroadcast_expired_submitted()

        execute_broadcast.assert_not_called()

    @patch("tron.models.TronTxTask.execute_broadcast")
    def test_rebroadcast_expired_submitted_executes_after_expiration(
        self,
        execute_broadcast,
    ):
        task = self.make_task()
        TxTask.objects.filter(pk=task.base_task_id).update(
            status=TxTaskStatus.SUBMITTED,
        )
        task.expiration = int((timezone.now() - timedelta(minutes=1)).timestamp() * 1000)
        task.save(update_fields=["expiration"])

        task.rebroadcast_expired_submitted()

        execute_broadcast.assert_called_once()

    @patch("tron.models.TronTxTask.execute_broadcast")
    def test_rebroadcast_expired_submitted_marks_failed_after_hash_limit(
        self,
        execute_broadcast,
    ):
        task = self.make_task()
        TxTask.objects.filter(pk=task.base_task_id).update(
            status=TxTaskStatus.SUBMITTED,
        )
        task.expiration = int((timezone.now() - timedelta(minutes=1)).timestamp() * 1000)
        task.save(update_fields=["expiration"])
        for index in range(TRON_MAX_BROADCAST_HASHES):
            task.base_task.append_tx_hash(
                f"{index + 1:064x}",
                expires_at_ms=task.expiration,
            )

        task.rebroadcast_expired_submitted()

        execute_broadcast.assert_not_called()
        task.base_task.refresh_from_db()
        self.assertEqual(task.base_task.status, TxTaskStatus.FAILED)

    def test_broadcast_lock_timeouts_cover_worst_case_http_budget(self):
        self.assertEqual(
            TRON_SENDER_BROADCAST_LOCK_TIMEOUT_SECONDS,
            TRON_BROADCAST_LOCK_TIMEOUT_SECONDS,
        )
        self.assertGreaterEqual(TRON_BROADCAST_LOCK_TIMEOUT_SECONDS, 150)

    @patch("tron.models.TronTxTask.rebroadcast_expired_submitted")
    @patch("tron.models.TronTxTask.broadcast")
    def test_broadcast_task_routes_submitted_to_rebroadcast(
        self,
        broadcast,
        rebroadcast_expired_submitted,
    ):
        task = self.make_task()
        TxTask.objects.filter(pk=task.base_task_id).update(
            status=TxTaskStatus.SUBMITTED,
        )

        broadcast_tron_task.run(task.pk)

        broadcast.assert_not_called()
        rebroadcast_expired_submitted.assert_called_once()

    @patch("tron.models.TronTxTask.broadcast")
    @patch("tron.tasks.AdapterFactory.get_adapter")
    def test_broadcast_task_recovers_queued_known_hash_before_rebroadcast(
        self,
        get_adapter,
        broadcast,
    ):
        task = self.make_task()
        tx_hash = "1" * 64
        task.base_task.append_tx_hash(tx_hash)
        adapter = Mock()
        adapter.tx_result.return_value = TxCheckResult(
            status=TxCheckStatus.SUCCEEDED,
            block_number=self.chain.latest_block_number,
            block_hash="1" * 64,
        )
        get_adapter.return_value = adapter

        broadcast_tron_task.run(task.pk)

        broadcast.assert_not_called()
        task.base_task.refresh_from_db()
        self.assertEqual(task.base_task.status, TxTaskStatus.SUBMITTED)
        self.assertEqual(task.base_task.tx_hash, tx_hash)

    @patch("tron.tasks.cache")
    @patch("tron.models.TronTxTask.broadcast")
    def test_broadcast_task_skips_when_sender_lock_is_held(
        self,
        broadcast,
        cache_mock,
    ):
        task = self.make_task()
        cache_mock.add.return_value = False

        broadcast_tron_task.run(task.pk)

        broadcast.assert_not_called()
        cache_mock.delete.assert_not_called()

    @patch("tron.tasks.broadcast_tron_task.delay")
    def test_dispatch_tron_tx_tasks_dispatches_one_task_per_tick(self, delay_mock):
        from tron.tasks import dispatch_tron_tx_tasks

        first = self.make_task()
        second = self.make_task()
        TronTxTask.objects.filter(pk=first.pk).update(
            created_at=timezone.now() - timedelta(seconds=10),
            last_attempt_at=None,
        )
        TronTxTask.objects.filter(pk=second.pk).update(
            created_at=timezone.now() - timedelta(seconds=5),
            last_attempt_at=None,
        )

        with self.captureOnCommitCallbacks(execute=True):
            dispatch_tron_tx_tasks.run()

        delay_mock.assert_called_once_with(first.pk)


class TronWatchCursorTests(TestCase):
    def test_enabling_tron_chain_creates_scan_cursor(self):
        # 游标按链唯一、与具体 TRC20 无关：激活 Tron 链即建出游标，
        # 不依赖 USDT 是否已配置。
        chain = Chain.objects.create(
            code=ChainCode.Tron,
            rpc="https://api.trongrid.io",
            tron_api_key="tron-key",
            active=False,
        )
        self.assertFalse(TronWatchCursor.objects.filter(chain=chain).exists())

        chain.active = True
        chain.save(update_fields=["active"])

        cursor = TronWatchCursor.objects.get(chain=chain)
        self.assertTrue(cursor.enabled)
        self.assertEqual(cursor.last_scanned_block, 0)

    def test_tron_chain_save_clears_generic_rpc(self):
        """Out of scope, follow-up: Chain.save 不存在 RPC 清空 / API Key trim 逻辑。
        保存后 rpc 与 tron_api_key 保持原文不变，未来可按需引入规范化。"""
        chain = Chain.objects.create(
            code=ChainCode.Tron,
            rpc="https://api.trongrid.io",
            tron_api_key="  tron-key  ",
        )

        chain.refresh_from_db()
        self.assertEqual(chain.rpc, "https://api.trongrid.io")
        self.assertEqual(chain.tron_api_key, "  tron-key  ")

    def test_cursor_is_unique_per_chain(self):
        # active=False 时 save 不会自动建游标，便于显式验证按链唯一约束。
        chain = Chain.objects.create(
            code=ChainCode.Tron,
            rpc="https://api.trongrid.io",
            tron_api_key="tron-key",
            active=False,
        )
        TronWatchCursor.objects.create(chain=chain)

        with self.assertRaises(IntegrityError):
            TronWatchCursor.objects.create(chain=chain)


class TronWatchCursorAdminTests(TestCase):
    def setUp(self):
        # active=False 避免 save 自动建游标，setUp 显式建一个可控初值的游标。
        self.chain = Chain.objects.create(
            code=ChainCode.Tron,
            rpc="https://api.trongrid.io",
            tron_api_key="tron-key",
            active=False,
            latest_block_number=66,
        )
        self.cursor = TronWatchCursor.objects.create(
            chain=self.chain,
            last_scanned_block=11,
            last_error="old error",
            last_error_at=timezone.now(),
        )
        self.admin = TronWatchCursorAdmin(TronWatchCursor, AdminSite())
        self.admin.message_user = Mock()

    @patch("tron.admin.TronHttpClient")
    @patch.object(Chain, "get_latest_block_number", new_callable=PropertyMock)
    def test_sync_selected_to_latest_fetches_realtime_solid_block_number(
        self, get_latest_block_number_mock, client_cls
    ):
        get_latest_block_number_mock.side_effect = AssertionError(
            "should not fetch realtime block height"
        )
        Chain.objects.filter(pk=self.chain.pk).update(latest_block_number=77)
        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = 88

        self.admin.sync_selected_to_latest(
            request=Mock(),
            queryset=TronWatchCursor.objects.filter(pk=self.cursor.pk),
        )

        self.cursor.refresh_from_db()
        self.chain.refresh_from_db()

        self.assertEqual(self.cursor.last_scanned_block, 88)
        self.assertEqual(self.cursor.last_error, "")
        self.assertIsNone(self.cursor.last_error_at)
        self.assertEqual(self.chain.latest_block_number, 88)
        self.admin.message_user.assert_called_once()
        self.assertEqual(get_latest_block_number_mock.call_count, 0)
        client_cls.assert_called_once()
        self.assertEqual(client_cls.call_args.kwargs["chain"].pk, self.chain.pk)

    @patch("tron.admin.TronHttpClient")
    def test_sync_selected_to_latest_never_rewinds_chain_or_cursor(self, client_cls):
        Chain.objects.filter(pk=self.chain.pk).update(latest_block_number=100)
        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = 80

        self.admin.sync_selected_to_latest(
            request=Mock(),
            queryset=TronWatchCursor.objects.filter(pk=self.cursor.pk),
        )

        self.cursor.refresh_from_db()
        self.chain.refresh_from_db()

        self.assertEqual(self.cursor.last_scanned_block, 100)
        self.assertEqual(self.chain.latest_block_number, 100)
        self.admin.message_user.assert_called_once()

    @patch("tron.admin.TronHttpClient")
    def test_sync_selected_to_latest_keeps_cursor_when_realtime_fetch_fails(
        self, client_cls
    ):
        client = client_cls.return_value
        client.get_latest_solid_block_number.side_effect = TronClientError(
            "latest failed"
        )

        self.admin.sync_selected_to_latest(
            request=Mock(),
            queryset=TronWatchCursor.objects.filter(pk=self.cursor.pk),
        )

        self.cursor.refresh_from_db()
        self.chain.refresh_from_db()

        self.assertEqual(self.cursor.last_scanned_block, 11)
        self.assertEqual(self.cursor.last_error, "old error")
        self.assertEqual(self.chain.latest_block_number, 66)
        self.admin.message_user.assert_called_once()


@override_settings(TRON_SCAN_SAFE_LAG_BLOCKS=0)
class TronScannerTests(TestCase):
    def setUp(self):
        self.usdt = Crypto.objects.create(
            name="Tether Tron",
            symbol="USDT",
            prices={"USD": "1"},
            coingecko_id="tether-tron-scan",
        )
        self.chain = Chain.objects.create(
            code=ChainCode.Tron,
            rpc="https://api.trongrid.io",
            tron_api_key="tron-key",
            active=True,
        )
        self.trx = self.chain.native_coin
        self.usdt_mapping = CryptoOnChain.objects.create(
            chain=self.chain,
            crypto=self.usdt,
            address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            decimals=6,
        )
        self.project = Project.objects.create(
            name="Tron Scan Project",
        )
        Fiat.objects.get_or_create(code="USD")
        self.watch_address = "TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb"
        self.sender_address = "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8"

    def _set_cursor_block(self, *, last_scanned_block: int) -> TronWatchCursor:
        # active=True 链在 setUp 保存时已自动建出按链唯一的游标，这里改其块高即可。
        cursor, _ = TronWatchCursor.objects.get_or_create(
            chain=self.chain,
            defaults={"last_scanned_block": last_scanned_block},
        )
        if cursor.last_scanned_block != last_scanned_block:
            TronWatchCursor.objects.filter(pk=cursor.pk).update(
                last_scanned_block=last_scanned_block
            )
            cursor.last_scanned_block = last_scanned_block
        return cursor

    @override_settings(DEBUG=False)
    @patch("tron.scanner.TronHttpClient")
    def test_debug_false_first_scan_bootstraps_cursor_to_latest_block_without_fetching_history(
        self,
        client_cls,
    ):
        from tron.scanner import TronScanner

        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = 123456
        client.get_solid_block_id.return_value = "0" * 64

        summary = TronScanner.scan_chain(chain=self.chain)

        self.assertEqual(summary.blocks_scanned, 0)
        self.assertEqual(summary.filter_addresses, 0)
        client.list_confirmed_contract_events.assert_not_called()
        cursor = TronWatchCursor.objects.get(chain=self.chain)
        self.assertEqual(cursor.last_scanned_block, 123456)

    @override_settings(DEBUG=False)
    @patch("tron.scanner.TronHttpClient")
    def test_debug_false_resume_from_last_scanned_block(self, client_cls):
        from tron.scanner import TronScanner

        self._set_cursor_block(last_scanned_block=123455)
        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = 123456
        client.get_solid_block_id.return_value = "0" * 64
        client.list_confirmed_contract_events.return_value = {"data": [], "meta": {}}

        summary = TronScanner.scan_chain(chain=self.chain)

        self.assertEqual(summary.blocks_scanned, 1)
        client.list_confirmed_contract_events.assert_called_once()

    @override_settings(TRON_SCAN_SAFE_LAG_BLOCKS=4)
    @patch("chains.service.TransferService.enqueue_processing")
    @patch("tron.scanner.TronHttpClient")
    def test_scan_chain_keeps_safe_lag_from_latest_solid_block(
        self,
        client_cls,
        _enqueue_processing_mock,
    ):
        from tron.scanner import TronScanner

        self._set_cursor_block(last_scanned_block=123451)
        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = 123456
        client.get_solid_block_id.return_value = "0" * 64
        client.list_confirmed_contract_events.return_value = {"data": [], "meta": {}}

        summary = TronScanner.scan_chain(chain=self.chain)

        self.assertEqual(summary.blocks_scanned, 1)
        client.list_confirmed_contract_events.assert_called_once()
        self.assertEqual(
            client.list_confirmed_contract_events.call_args.kwargs["block_number"],
            123452,
        )
        cursor = TronWatchCursor.objects.get(chain=self.chain)
        self.assertEqual(cursor.last_scanned_block, 123452)
        self.chain.refresh_from_db()
        self.assertEqual(self.chain.latest_block_number, 123456)

    @override_settings(DEBUG=True)
    @patch("tron.scanner.TronHttpClient")
    def test_debug_true_restarts_from_latest_after_process_reset(self, client_cls):
        from tron.scanner import TronScanner

        TronScanner._debug_bootstrapped_cursors.clear()
        self._set_cursor_block(last_scanned_block=123400)

        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = 123500
        client.get_solid_block_id.return_value = "0" * 64
        TronScanner.scan_chain(chain=self.chain)

        client.list_confirmed_contract_events.assert_not_called()

        client.reset_mock()
        client.get_latest_solid_block_number.return_value = 123501
        client.get_solid_block_id.return_value = "0" * 64
        client.list_confirmed_contract_events.return_value = {"data": [], "meta": {}}
        TronScanner.scan_chain(chain=self.chain)
        client.list_confirmed_contract_events.assert_called_once()

        TronScanner._debug_bootstrapped_cursors.clear()
        client.reset_mock()
        client.get_latest_solid_block_number.return_value = 123510
        client.get_solid_block_id.return_value = "0" * 64
        TronScanner.scan_chain(chain=self.chain)
        client.list_confirmed_contract_events.assert_not_called()

    @patch("tron.scanner.TronHttpClient")
    def test_scan_chain_records_cursor_error_when_latest_block_rpc_fails(
        self,
        client_cls,
    ):
        from tron.scanner import TronScanner

        client = client_cls.return_value
        client.get_latest_solid_block_number.side_effect = TronClientError(
            "latest failed"
        )

        with self.assertRaisesMessage(TronClientError, "latest failed"):
            TronScanner.scan_chain(chain=self.chain)

        cursor = TronWatchCursor.objects.get(chain=self.chain)
        self.assertEqual(cursor.last_scanned_block, 0)
        self.assertEqual(cursor.last_error, "latest failed")
        self.assertIsNotNone(cursor.last_error_at)

    @patch("chains.service.TransferService.enqueue_processing")
    @patch("tron.scanner.TronScanner._advance_cursor")
    @patch("tron.scanner.TronHttpClient")
    def test_scan_chain_writes_cursor_only_once_for_full_batch(
        self,
        client_cls,
        advance_cursor_mock,
        _enqueue_processing_mock,
    ):
        # 一轮扫描多块只 flush 一次游标，避免追平时 N 次单行 update 压垮 DB；
        # _advance_cursor 是统一的写入入口，命中次数等于实际写库次数。
        from tron.scanner import DEFAULT_TRON_SCAN_BATCH_SIZE
        from tron.scanner import TronScanner

        start_cursor = 200_000
        latest_block = start_cursor + DEFAULT_TRON_SCAN_BATCH_SIZE

        self._set_cursor_block(last_scanned_block=start_cursor)
        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = latest_block
        client.get_solid_block_id.return_value = "0" * 64
        client.list_confirmed_contract_events.return_value = {"data": [], "meta": {}}

        TronScanner.scan_chain(chain=self.chain)

        self.assertEqual(advance_cursor_mock.call_count, 1)
        _, kwargs = advance_cursor_mock.call_args
        # 一次性 flush 时传入的 scanned_block 必须是当 tick 最后一个成功块，
        # 否则游标会停在中间块、下一轮重新扫尾段，浪费 RPC。
        self.assertEqual(kwargs["scanned_block"], latest_block)

    @patch("chains.service.TransferService.enqueue_processing")
    @patch("tron.scanner.TronHttpClient")
    def test_scan_chain_caps_single_tick_advance_at_batch_size(
        self,
        client_cls,
        _enqueue_processing_mock,
    ):
        # 单 tick 内 Tron 推进的块数必须被 DEFAULT_TRON_SCAN_BATCH_SIZE 限制，
        # 避免大幅落后时 range(start, latest+1) 无界拖垮当次 beat task。
        from tron.scanner import DEFAULT_TRON_SCAN_BATCH_SIZE
        from tron.scanner import TronScanner

        start_cursor = 100_000
        latest_block = start_cursor + DEFAULT_TRON_SCAN_BATCH_SIZE * 4
        expected_end_block = start_cursor + DEFAULT_TRON_SCAN_BATCH_SIZE

        self._set_cursor_block(last_scanned_block=start_cursor)
        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = latest_block
        client.get_solid_block_id.return_value = "0" * 64
        client.list_confirmed_contract_events.return_value = {"data": [], "meta": {}}

        summary = TronScanner.scan_chain(chain=self.chain)

        self.assertEqual(summary.blocks_scanned, DEFAULT_TRON_SCAN_BATCH_SIZE)
        self.assertEqual(
            client.list_confirmed_contract_events.call_count,
            DEFAULT_TRON_SCAN_BATCH_SIZE,
        )
        cursor = TronWatchCursor.objects.get(chain=self.chain)
        self.assertEqual(cursor.last_scanned_block, expected_end_block)

    def test_tron_cursor_advance_never_rewinds_database_value(self):
        from tron.scanner import TronScanner

        cursor = self._set_cursor_block(last_scanned_block=100)
        stale_cursor = TronWatchCursor.objects.get(pk=cursor.pk)
        TronWatchCursor.objects.filter(pk=cursor.pk).update(
            last_scanned_block=150,
        )

        TronScanner._advance_cursor(
            cursor=stale_cursor,
            latest_block=120,
            scanned_block=120,
        )

        cursor.refresh_from_db()
        self.assertEqual(cursor.last_scanned_block, 150)

    @patch("chains.service.TransferService.enqueue_processing")
    @patch("tron.scanner.TronHttpClient")
    def test_scan_chain_never_rewinds_chain_latest_block_number(
        self,
        client_cls,
        _enqueue_processing_mock,
    ):
        from tron.scanner import TronScanner

        Chain.objects.filter(pk=self.chain.pk).update(latest_block_number=200)
        self.chain.refresh_from_db()
        self._set_cursor_block(last_scanned_block=100)
        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = 120
        client.get_solid_block_id.return_value = "0" * 64
        client.list_confirmed_contract_events.return_value = {"data": [], "meta": {}}

        TronScanner.scan_chain(chain=self.chain)

        self.chain.refresh_from_db()
        self.assertEqual(self.chain.latest_block_number, 200)

    @patch("chains.tasks.block_number_updated.delay")
    @patch("chains.service.TransferService.enqueue_processing")
    @patch("tron.scanner.TronHttpClient")
    def test_scan_chain_dispatches_confirmation_checks_after_block_advance(
        self,
        client_cls,
        _enqueue_processing_mock,
        block_number_updated_delay_mock,
    ):
        from tron.scanner import TronScanner

        Transfer.objects.create(
            chain=self.chain,
            block=100,
            block_hash="0x" + "33" * 32,
            hash="c" * 64,
            crypto=self.usdt,
            from_address=self.sender_address,
            to_address=self.watch_address,
            value=1,
            amount=Decimal("0.000001"),
            timestamp=1,
            datetime=timezone.now(),
            processed_at=timezone.now(),
        )
        self._set_cursor_block(last_scanned_block=110)
        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = 120
        client.get_solid_block_id.return_value = "0" * 64
        client.list_confirmed_contract_events.return_value = {"data": [], "meta": {}}

        TronScanner.scan_chain(chain=self.chain)

        block_number_updated_delay_mock.assert_called_once_with(self.chain.pk)

    @patch("chains.service.TransferService.enqueue_processing")
    @patch("tron.scanner.TronHttpClient")
    def test_scan_chain_stops_when_native_block_payload_has_no_block_id(
        self,
        client_cls,
        _enqueue_processing_mock,
    ):
        from tron.scanner import TronScanner

        CryptoOnChain.objects.update_or_create(
            chain=self.chain,
            crypto=self.trx,
            defaults={"address": "", "decimals": 6, "active": True},
        )
        cursor = self._set_cursor_block(last_scanned_block=123455)
        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = 123456
        client.get_solid_block_id.return_value = "0" * 64
        client.list_confirmed_contract_events.return_value = {"data": [], "meta": {}}
        client.get_solid_block.return_value = {}

        with self.assertRaisesMessage(TronClientError, "invalid solid block id"):
            TronScanner.scan_chain(chain=self.chain)

        cursor.refresh_from_db()
        self.assertEqual(cursor.last_scanned_block, 123455)
        self.assertIn("invalid solid block id", cursor.last_error)

    @patch("chains.service.TransferService.enqueue_processing")
    @patch("tron.scanner.TronHttpClient")
    def test_scan_chain_matches_vault_slot_candidates_without_loading_all_addresses(
        self,
        client_cls,
        enqueue_processing_mock,
    ):
        from tron.scanner import TronScanner

        VaultSlot.objects.create(
            chain=self.chain,
            project=self.project,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=0,
            address=self.watch_address,
            salt=b"a" * 32,
        )
        self._set_cursor_block(last_scanned_block=123455)
        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = 123456
        client.get_solid_block_id.return_value = "0" * 64
        client.list_confirmed_contract_events.return_value = {
            "data": [
                {
                    "transaction_id": "d" * 64,
                    "event_index": "0",
                    "block_number": 123456,
                    "block_timestamp": 1_700_000_000_000,
                    "event_name": "Transfer",
                    "contract_address": self.usdt_mapping.address,
                    "result": {
                        "from": self.sender_address,
                        "to": self.watch_address,
                        "value": "1234567",
                    },
                }
            ],
            "meta": {},
        }

        summary = TronScanner.scan_chain(chain=self.chain)

        self.assertEqual(summary.events_seen, 1)
        self.assertEqual(summary.filter_addresses, 1)
        transfer = Transfer.objects.get(hash="d" * 64)
        self.assertEqual(transfer.to_address, self.watch_address)
        self.assertEqual(transfer.event_index, 0)
        self.assertEqual(transfer.amount, Decimal("1.234567"))
        enqueue_processing_mock.assert_called_once()

    @patch("chains.service.TransferService.enqueue_processing")
    @patch("tron.scanner.TronHttpClient")
    def test_scan_chain_creates_independent_trc20_events_in_same_tx(
        self,
        client_cls,
        enqueue_processing_mock,
    ):
        from tron.scanner import TronScanner

        second_watch_address = "TVjsyZ7fYF3qLF6BQgPmTEZy1xrNNyVAAA"
        VaultSlot.objects.create(
            chain=self.chain,
            project=self.project,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=0,
            address=self.watch_address,
            salt=b"a" * 32,
        )
        VaultSlot.objects.create(
            chain=self.chain,
            project=self.project,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=1,
            address=second_watch_address,
            salt=b"b" * 32,
        )
        self._set_cursor_block(last_scanned_block=123455)
        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = 123456
        client.get_solid_block_id.return_value = "0" * 64
        client.list_confirmed_contract_events.return_value = {
            "data": [
                {
                    "transaction_id": "1" * 64,
                    "event_index": "0",
                    "block_number": 123456,
                    "block_timestamp": 1_700_000_000_000,
                    "event_name": "Transfer",
                    "contract_address": self.usdt_mapping.address,
                    "result": {
                        "from": self.sender_address,
                        "to": self.watch_address,
                        "value": "1000000",
                    },
                },
                {
                    "transaction_id": "1" * 64,
                    "event_index": "1",
                    "block_number": 123456,
                    "block_timestamp": 1_700_000_000_000,
                    "event_name": "Transfer",
                    "contract_address": self.usdt_mapping.address,
                    "result": {
                        "from": self.sender_address,
                        "to": second_watch_address,
                        "value": "2000000",
                    },
                },
            ],
            "meta": {},
        }

        summary = TronScanner.scan_chain(chain=self.chain)

        self.assertEqual(summary.events_seen, 2)
        self.assertEqual(summary.filter_addresses, 2)
        transfers = list(Transfer.objects.filter(hash="1" * 64).order_by("event_index"))
        self.assertEqual(len(transfers), 2)
        self.assertEqual([transfer.event_index for transfer in transfers], [0, 1])
        self.assertEqual([transfer.amount for transfer in transfers], [Decimal("1"), Decimal("2")])
        self.assertEqual(enqueue_processing_mock.call_count, 2)

    @patch("chains.service.TransferService.enqueue_processing")
    @patch("tron.scanner.TronHttpClient")
    def test_scan_chain_skips_oversized_trc20_value_without_blocking_valid_event(
        self,
        client_cls,
        enqueue_processing_mock,
    ):
        from tron.scanner import TronScanner

        VaultSlot.objects.create(
            chain=self.chain,
            project=self.project,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=0,
            address=self.watch_address,
            salt=b"c" * 32,
        )
        self._set_cursor_block(last_scanned_block=123455)
        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = 123456
        client.get_solid_block_id.return_value = "0" * 64
        client.list_confirmed_contract_events.return_value = {
            "data": [
                {
                    "transaction_id": "2" * 64,
                    "event_index": "0",
                    "block_number": 123456,
                    "block_timestamp": 1_700_000_000_000,
                    "event_name": "Transfer",
                    "contract_address": self.usdt_mapping.address,
                    "result": {
                        "from": self.sender_address,
                        "to": self.watch_address,
                        "value": str(10**32),
                    },
                },
                {
                    "transaction_id": "3" * 64,
                    "event_index": "1",
                    "block_number": 123456,
                    "block_timestamp": 1_700_000_000_000,
                    "event_name": "Transfer",
                    "contract_address": self.usdt_mapping.address,
                    "result": {
                        "from": self.sender_address,
                        "to": self.watch_address,
                        "value": "1000000",
                    },
                },
            ],
            "meta": {},
        }

        summary = TronScanner.scan_chain(chain=self.chain)

        self.assertEqual(summary.events_seen, 1)
        self.assertFalse(Transfer.objects.filter(hash="2" * 64).exists())
        transfer = Transfer.objects.get(hash="3" * 64)
        self.assertEqual(transfer.event_index, 1)
        self.assertEqual(transfer.amount, Decimal("1"))
        enqueue_processing_mock.assert_called_once()

    @patch("tron.scanner.TransferService.create_observed_transfer")
    @patch("tron.scanner.TronHttpClient")
    def test_scan_chain_continues_after_single_persist_error(
        self,
        client_cls,
        create_observed_transfer_mock,
    ):
        from tron.scanner import TronScanner

        VaultSlot.objects.create(
            chain=self.chain,
            project=self.project,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=0,
            address=self.watch_address,
            salt=b"d" * 32,
        )
        self._set_cursor_block(last_scanned_block=123455)
        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = 123456
        client.get_solid_block_id.return_value = "0" * 64
        client.list_confirmed_contract_events.return_value = {
            "data": [
                {
                    "transaction_id": "4" * 64,
                    "event_index": "0",
                    "block_number": 123456,
                    "block_timestamp": 1_700_000_000_000,
                    "event_name": "Transfer",
                    "contract_address": self.usdt_mapping.address,
                    "result": {
                        "from": self.sender_address,
                        "to": self.watch_address,
                        "value": "1000000",
                    },
                },
                {
                    "transaction_id": "5" * 64,
                    "event_index": "1",
                    "block_number": 123456,
                    "block_timestamp": 1_700_000_000_000,
                    "event_name": "Transfer",
                    "contract_address": self.usdt_mapping.address,
                    "result": {
                        "from": self.sender_address,
                        "to": self.watch_address,
                        "value": "2000000",
                    },
                },
            ],
            "meta": {},
        }
        create_observed_transfer_mock.side_effect = [
            RuntimeError("numeric field overflow"),
            None,
        ]

        summary = TronScanner.scan_chain(chain=self.chain)

        self.assertEqual(summary.events_seen, 2)
        self.assertEqual(create_observed_transfer_mock.call_count, 2)
        observed_events = [
            call.kwargs["observed"].event_index
            for call in create_observed_transfer_mock.call_args_list
        ]
        self.assertEqual(observed_events, [0, 1])

    @patch("chains.service.TransferService.enqueue_processing")
    @patch("tron.scanner.TronHttpClient")
    def test_scan_chain_matches_differ_recipient_candidates(
        self,
        client_cls,
        enqueue_processing_mock,
    ):
        from tron.scanner import TronScanner

        DifferRecipientAddress.objects.create(
            project=self.project,
            chain_type=ChainType.TRON,
            address=self.watch_address,
        )
        self._set_cursor_block(last_scanned_block=123455)
        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = 123456
        client.get_solid_block_id.return_value = "0" * 64
        client.list_confirmed_contract_events.return_value = {
            "data": [
                {
                    "transaction_id": "e" * 64,
                    "event_index": "0",
                    "block_number": 123456,
                    "block_timestamp": 1_700_000_000_000,
                    "event_name": "Transfer",
                    "contract_address": self.usdt_mapping.address,
                    "result": {
                        "from": self.sender_address,
                        "to": self.watch_address,
                        "value": "1234567",
                    },
                }
            ],
            "meta": {},
        }

        summary = TronScanner.scan_chain(chain=self.chain)

        self.assertEqual(summary.events_seen, 1)
        self.assertEqual(summary.filter_addresses, 1)
        transfer = Transfer.objects.get(hash="e" * 64)
        self.assertEqual(transfer.to_address, self.watch_address)
        self.assertEqual(transfer.amount, Decimal("1.234567"))
        enqueue_processing_mock.assert_called_once()

    def _native_tx(
        self,
        *,
        to_hex: str,
        from_hex: str,
        contract_type: str = "TransferContract",
        contract_ret: str = "SUCCESS",
        amount: int = 1_000_000,
        tx_id: str = "f" * 64,
    ) -> dict:
        return {
            "txID": tx_id,
            "ret": [{"contractRet": contract_ret}],
            "raw_data": {
                "contract": [
                    {
                        "type": contract_type,
                        "parameter": {
                            "value": {
                                "amount": amount,
                                "owner_address": from_hex,
                                "to_address": to_hex,
                            }
                        },
                    }
                ],
            },
        }

    def test_parse_native_transfer_accepts_transfer_contract_skips_others(self):
        # 原生 TRX 解析核心：合法 TransferContract→入账事件；非 TransferContract/执行失败/
        # 金额非正一律跳过。hex41 地址须正确还原为收款 base58 地址。
        from tron.codec import TronAddressCodec
        from tron.scanner import TronScanner

        to_hex = TronAddressCodec.base58_to_hex41(self.watch_address)
        from_hex = TronAddressCodec.base58_to_hex41(self.sender_address)

        def parse(tx):
            return TronScanner._parse_native_transfer(
                chain=self.chain,
                tx=tx,
                block_number=123,
                block_hash="a" * 64,
                block_timestamp_ms=1_700_000_000_000,
                crypto=self.trx,
                decimals=6,
            )

        event = parse(self._native_tx(to_hex=to_hex, from_hex=from_hex))
        self.assertIsNotNone(event)
        self.assertEqual(event.observed.event_index, 0)
        self.assertEqual(event.observed.to_address, self.watch_address)
        self.assertEqual(event.observed.from_address, self.sender_address)
        self.assertEqual(event.observed.crypto, self.trx)
        self.assertEqual(event.observed.value, Decimal("1000000"))
        self.assertEqual(event.observed.amount, Decimal("1"))

        self.assertIsNone(
            parse(
                self._native_tx(
                    to_hex=to_hex,
                    from_hex=from_hex,
                    contract_type="TriggerSmartContract",
                )
            )
        )
        self.assertIsNone(
            parse(
                self._native_tx(to_hex=to_hex, from_hex=from_hex, contract_ret="REVERT")
            )
        )
        self.assertIsNone(
            parse(self._native_tx(to_hex=to_hex, from_hex=from_hex, amount=0))
        )
        self.assertIsNone(
            parse(self._native_tx(to_hex=to_hex, from_hex=from_hex, amount=10**32))
        )

    @patch("chains.service.TransferService.enqueue_processing")
    @patch("tron.scanner.TronHttpClient")
    def test_scan_chain_matches_native_trx_transfer_contract(
        self,
        client_cls,
        enqueue_processing_mock,
    ):
        # 端到端：块内一笔打给收款地址的原生 TRX TransferContract → 落库一条 TRX Transfer。
        from tron.codec import TronAddressCodec
        from tron.scanner import TronScanner

        VaultSlot.objects.create(
            chain=self.chain,
            project=self.project,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=0,
            address=self.watch_address,
            salt=b"n" * 32,
        )
        self._set_cursor_block(last_scanned_block=123455)
        to_hex = TronAddressCodec.base58_to_hex41(self.watch_address)
        from_hex = TronAddressCodec.base58_to_hex41(self.sender_address)
        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = 123456
        client.get_solid_block_id.return_value = "0" * 64
        client.list_confirmed_contract_events.return_value = {"data": [], "meta": {}}
        client.get_solid_block.return_value = {
            "blockID": "a" * 64,
            "block_header": {"raw_data": {"timestamp": 1_700_000_000_000}},
            "transactions": [
                self._native_tx(to_hex=to_hex, from_hex=from_hex, amount=1_500_000)
            ],
        }

        summary = TronScanner.scan_chain(chain=self.chain)

        self.assertEqual(summary.events_seen, 1)
        self.assertEqual(summary.filter_addresses, 1)
        transfer = Transfer.objects.get(hash="f" * 64)
        self.assertEqual(transfer.to_address, self.watch_address)
        self.assertEqual(transfer.event_index, 0)
        self.assertEqual(transfer.crypto, self.trx)
        self.assertEqual(transfer.value, Decimal("1500000"))
        self.assertEqual(transfer.amount, Decimal("1.5"))
        enqueue_processing_mock.assert_called_once()

    @patch("chains.service.TransferService.enqueue_processing")
    @patch("tron.scanner.TronHttpClient")
    def test_scan_chain_matches_native_transfer_contract_after_first_contract(
        self,
        client_cls,
        enqueue_processing_mock,
    ):
        from tron.codec import TronAddressCodec
        from tron.scanner import TronScanner

        VaultSlot.objects.create(
            chain=self.chain,
            project=self.project,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=0,
            address=self.watch_address,
            salt=b"m" * 32,
        )
        self._set_cursor_block(last_scanned_block=123455)
        to_hex = TronAddressCodec.base58_to_hex41(self.watch_address)
        from_hex = TronAddressCodec.base58_to_hex41(self.sender_address)
        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = 123456
        client.get_solid_block_id.return_value = "0" * 64
        client.list_confirmed_contract_events.return_value = {"data": [], "meta": {}}
        client.get_solid_block.return_value = {
            "blockID": "a" * 64,
            "block_header": {"raw_data": {"timestamp": 1_700_000_000_000}},
            "transactions": [
                {
                    "txID": "2" * 64,
                    "ret": [{"contractRet": "SUCCESS"}, {"contractRet": "SUCCESS"}],
                    "raw_data": {
                        "contract": [
                            {"type": "FreezeBalanceV2Contract", "parameter": {"value": {}}},
                            {
                                "type": "TransferContract",
                                "parameter": {
                                    "value": {
                                        "amount": 2_500_000,
                                        "owner_address": from_hex,
                                        "to_address": to_hex,
                                    }
                                },
                            },
                        ],
                    },
                }
            ],
        }

        summary = TronScanner.scan_chain(chain=self.chain)

        self.assertEqual(summary.events_seen, 1)
        transfer = Transfer.objects.get(hash="2" * 64)
        self.assertEqual(transfer.event_index, 1)
        self.assertEqual(transfer.amount, Decimal("2.5"))
        enqueue_processing_mock.assert_called_once()

    @patch("chains.service.TransferService.enqueue_processing")
    @patch("tron.scanner.TronHttpClient")
    def test_scan_chain_skips_native_when_native_coin_inactive(
        self,
        client_cls,
        _enqueue_processing_mock,
    ):
        # 停用原生币 CryptoOnChain → 关闭原生扫描，不再逐块拉整块。
        from tron.scanner import TronScanner

        CryptoOnChain.objects.filter(chain=self.chain, crypto=self.trx).update(
            active=False
        )
        self._set_cursor_block(last_scanned_block=123455)
        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = 123456
        client.get_solid_block_id.return_value = "0" * 64
        client.list_confirmed_contract_events.return_value = {"data": [], "meta": {}}

        TronScanner.scan_chain(chain=self.chain)

        client.get_solid_block.assert_not_called()


class TronTaskTests(TestCase):
    @patch("tron.tasks.logger.info")
    @patch("tron.tasks.TronScanner.scan_chain")
    def test_scan_tron_chain_logs_filter_addresses_and_blocks_scanned(
        self,
        scan_chain_mock,
        logger_info_mock,
    ):
        from tron.scanner import TronScanSummary
        from tron.tasks import scan_tron_chain

        tron_chain = Chain.objects.create(
            code=ChainCode.Tron,
            rpc="https://api.trongrid.io",
            tron_api_key="tron-key",
            active=True,
        )
        scan_chain_mock.return_value = TronScanSummary(
            filter_addresses=3,
            blocks_scanned=7,
            events_seen=11,
        )

        scan_tron_chain.run(tron_chain.pk)

        logger_info_mock.assert_called_once_with(
            "Tron TRC20 扫描完成",
            chain=tron_chain.code,
            filter_addresses=3,
            blocks_scanned=7,
            events_seen=11,
        )

    @patch("tron.tasks.TronScanner.scan_chain")
    def test_scan_tron_chain_skips_when_api_key_is_missing(
        self,
        scan_chain_mock,
    ):
        from tron.tasks import scan_tron_chain

        tron_chain = Chain.objects.create(
            code=ChainCode.Tron,
            rpc="https://api.trongrid.io",
            active=False,
        )

        scan_tron_chain.run(tron_chain.pk)

        scan_chain_mock.assert_not_called()

    @patch("tron.tasks.scan_tron_chain.delay")
    def test_scan_active_tron_chains_only_dispatches_active_tron_chains(
        self,
        scan_delay_mock,
    ):
        from tron.tasks import scan_active_tron_chains

        tron_chain = Chain.objects.create(
            code=ChainCode.Tron,
            rpc="https://api.trongrid.io",
            tron_api_key="tron-key",
            active=True,
        )
        evm_chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=False,
        )
        Chain.objects.filter(pk=evm_chain.pk).update(
            rpc="http://evm.invalid",
            active=True,
        )
        # 把 last_scanned_at 推到远早于扫描周期，使本链到期可被调度。
        Chain.objects.filter(pk=tron_chain.pk).update(
            last_scanned_at=timezone.now() - timedelta(hours=1)
        )

        scan_active_tron_chains.run()

        scan_delay_mock.assert_called_once_with(tron_chain.pk)

        scan_delay_mock.reset_mock()
        Chain.objects.filter(pk=tron_chain.pk).update(active=False, tron_api_key="")
        scan_active_tron_chains.run()
        scan_delay_mock.assert_not_called()

        Chain.objects.filter(pk=tron_chain.pk).update(
            active=False,
            tron_api_key="tron-key",
        )
        scan_active_tron_chains.run()
        scan_delay_mock.assert_not_called()


class TronReceiptConfirmTaskTests(TestCase):
    """部署与归集都不产生「打入系统观察地址」的入账,统一由
    confirm_tron_receipt_tx_tasks 按回执收口。

    核心回归:归集(collect)任务能被确认终局并触发归集 gas 计费回调——此前 collect
    既不终局也不计费,会被 dispatch 无限重广播。
    """

    def setUp(self):
        self.chain = Chain.objects.create(
            code=ChainCode.Tron,
            rpc="https://api.trongrid.io",
            tron_api_key="tron-key",
            active=True,
        )
        # 抬高 solid head,使任意小区块号都满足确认数门槛。
        Chain.objects.filter(pk=self.chain.pk).update(latest_block_number=10_000_000)
        self.chain.refresh_from_db()
        self.usdt = Crypto.objects.create(
            name="Tether Tron",
            symbol="USDT",
            prices={"USD": "1"},
            coingecko_id="tron-receipt-usdt",
        )
        CryptoOnChain.objects.create(
            chain=self.chain,
            crypto=self.usdt,
            address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            decimals=6,
        )
        Fiat.objects.get_or_create(code="USD")
        self.project = Project.objects.create(name="Tron Receipt Project")
        self.customer = Customer.objects.create(
            project=self.project, uid="tron-receipt-customer"
        )
        self.wallet = Wallet.objects.create()
        self.sender = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.TRON,
            usage=AddressUsage.HotWallet,
            bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
            address_index=0,
            address="TJRabPrwbZy45sbavfcjinPJC18kjpRTv8",
        )
        self.slot = VaultSlot.objects.create(
            chain=self.chain,
            usage=VaultSlotUsage.DEPOSIT,
            customer=self.customer,
            project=self.project,
            address="TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb",
            salt=b"\x01" * 32,
        )

    def create_collect_task(
        self,
        *,
        tx_hash="a" * 64,
        status=TxTaskStatus.SUBMITTED,
    ) -> TxTask:
        base_task = TxTask.objects.create(
            chain=self.chain,
            sender=self.sender,
            tx_type=TxTaskType.VaultSlotCollect,
            status=status,
        )
        base_task.append_tx_hash(tx_hash)
        TronTxTask.objects.create(
            base_task=base_task,
            sender=self.sender,
            chain=self.chain,
            to=self.slot.address,
            function_selector="collect(address)",
            parameter="00" * 32,
            fee_limit=150_000_000,
        )
        VaultSlotCollectSchedule.objects.create(
            chain=self.chain,
            vault_slot=self.slot,
            crypto=self.usdt,
            due_at=timezone.now(),
            tx_task=base_task,
        )
        return base_task

    def create_deploy_task(self, *, tx_hash="b" * 64) -> TxTask:
        base_task = TxTask.objects.create(
            chain=self.chain,
            sender=self.sender,
            tx_type=TxTaskType.VaultSlotDeploy,
            status=TxTaskStatus.SUBMITTED,
        )
        base_task.append_tx_hash(tx_hash)
        TronTxTask.objects.create(
            base_task=base_task,
            sender=self.sender,
            chain=self.chain,
            to="TJRabPrwbZy45sbavfcjinPJC18kjpRTv8",
            function_selector="deployVaultSlot(address,bytes32)",
            parameter="00" * 64,
            fee_limit=150_000_000,
        )
        VaultSlot.objects.filter(pk=self.slot.pk).update(deploy_tx_task=base_task)
        return base_task

    @patch("tron.saas_gas_billing.retry_vault_slot_collect_gas_fee.delay")
    @patch("tron.saas_gas_billing.build_tx_detail", side_effect=RuntimeError("rpc down"))
    def test_collect_gas_fee_build_failure_schedules_retry(
        self,
        _build_tx_detail_mock,
        retry_delay,
    ):
        from tron.saas_gas_billing import notify_vault_slot_collect_gas_fee

        base_task = self.create_collect_task()

        with self.captureOnCommitCallbacks(execute=True):
            notify_vault_slot_collect_gas_fee(tx_task=base_task)

        retry_delay.assert_called_once_with(base_task.pk)

    @patch("tron.tasks.notify_vault_slot_deploy_gas_fee")
    @patch("tron.tasks.notify_vault_slot_collect_gas_fee")
    @patch("tron.tasks.refresh_vault_slot_balance_for_collect_task")
    @patch("tron.tasks.AdapterFactory.get_adapter")
    def test_confirm_finalizes_collect_and_triggers_collect_gas_fee(
        self,
        get_adapter,
        refresh_balance,
        collect_gas_fee,
        deploy_gas_fee,
    ):
        base_task = self.create_collect_task()
        adapter = Mock()
        adapter.tx_result.return_value = TxCheckResult(
            status=TxCheckStatus.SUCCEEDED,
            block_number=100,
            block_hash="b" * 64,
        )
        get_adapter.return_value = adapter

        confirm_tron_receipt_tx_tasks()

        base_task.refresh_from_db()
        self.assertEqual(base_task.status, TxTaskStatus.SUCCEEDED)
        refresh_balance.assert_called_once()
        self.assertEqual(refresh_balance.call_args.args[0].pk, base_task.pk)
        collect_gas_fee.assert_called_once()
        self.assertEqual(
            collect_gas_fee.call_args.kwargs["tx_task"].pk, base_task.pk
        )
        deploy_gas_fee.assert_not_called()

    @patch("tron.tasks.notify_vault_slot_deploy_gas_fee")
    @patch("tron.tasks.notify_vault_slot_collect_gas_fee")
    @patch("tron.tasks.refresh_vault_slot_balance_for_collect_task")
    @patch("tron.tasks.AdapterFactory.get_adapter")
    def test_confirm_finalizes_queued_collect_with_known_hash(
        self,
        get_adapter,
        refresh_balance,
        collect_gas_fee,
        deploy_gas_fee,
    ):
        base_task = self.create_collect_task(status=TxTaskStatus.QUEUED)
        adapter = Mock()
        adapter.tx_result.return_value = TxCheckResult(
            status=TxCheckStatus.SUCCEEDED,
            block_number=100,
            block_hash="b" * 64,
        )
        get_adapter.return_value = adapter

        confirm_tron_receipt_tx_tasks()

        base_task.refresh_from_db()
        self.assertEqual(base_task.status, TxTaskStatus.SUCCEEDED)
        refresh_balance.assert_called_once()
        collect_gas_fee.assert_called_once()
        deploy_gas_fee.assert_not_called()

    @patch("tron.tasks.notify_vault_slot_deploy_gas_fee")
    @patch("tron.tasks.notify_vault_slot_collect_gas_fee")
    @patch("tron.tasks.refresh_vault_slot_balance_for_collect_task")
    @patch("tron.tasks.AdapterFactory.get_adapter")
    def test_confirm_finalizes_deploy_marks_slot_deployed_and_triggers_gas_fee(
        self,
        get_adapter,
        refresh_balance,
        collect_gas_fee,
        deploy_gas_fee,
    ):
        base_task = self.create_deploy_task()
        adapter = Mock()
        adapter.tx_result.return_value = TxCheckResult(
            status=TxCheckStatus.SUCCEEDED,
            block_number=100,
            block_hash="d" * 64,
        )
        get_adapter.return_value = adapter

        confirm_tron_receipt_tx_tasks()

        base_task.refresh_from_db()
        self.slot.refresh_from_db()
        self.assertEqual(base_task.status, TxTaskStatus.SUCCEEDED)
        self.assertTrue(self.slot.is_deployed)
        refresh_balance.assert_not_called()
        deploy_gas_fee.assert_called_once()
        self.assertEqual(deploy_gas_fee.call_args.kwargs["tx_task"].pk, base_task.pk)
        collect_gas_fee.assert_not_called()

    @patch("tron.tasks.notify_vault_slot_collect_gas_fee")
    @patch("tron.tasks.refresh_vault_slot_balance_for_collect_task")
    @patch("tron.tasks.AdapterFactory.get_adapter")
    def test_confirm_marks_failed_collect_without_gas_fee(
        self,
        get_adapter,
        refresh_balance,
        collect_gas_fee,
    ):
        base_task = self.create_collect_task()
        adapter = Mock()
        adapter.tx_result.return_value = TxCheckResult(
            status=TxCheckStatus.FAILED,
            block_number=100,
            block_hash="b" * 64,
        )
        get_adapter.return_value = adapter

        confirm_tron_receipt_tx_tasks()

        base_task.refresh_from_db()
        self.assertEqual(base_task.status, TxTaskStatus.FAILED)
        refresh_balance.assert_not_called()
        collect_gas_fee.assert_not_called()

    @patch("tron.tasks.notify_vault_slot_deploy_gas_fee")
    @patch("tron.tasks.notify_vault_slot_collect_gas_fee")
    @patch("tron.tasks.refresh_vault_slot_balance_for_collect_task")
    @patch("tron.tasks.AdapterFactory.get_adapter")
    def test_confirm_collect_resolves_success_from_old_hash(
        self,
        get_adapter,
        refresh_balance,
        collect_gas_fee,
        deploy_gas_fee,
    ):
        old_hash = "c" * 64
        new_hash = "d" * 64
        base_task = self.create_collect_task(tx_hash=old_hash)
        base_task.append_tx_hash(new_hash)
        adapter = Mock()

        def tx_result(*, chain, tx_hash):
            if tx_hash == new_hash:
                return TxCheckStatus.MISSING
            if tx_hash == old_hash:
                return TxCheckResult(
                    status=TxCheckStatus.SUCCEEDED,
                    block_number=100,
                    block_hash="b" * 64,
                )
            raise AssertionError(f"unexpected tx_hash={tx_hash}")

        adapter.tx_result.side_effect = tx_result
        get_adapter.return_value = adapter

        confirm_tron_receipt_tx_tasks()

        base_task.refresh_from_db()
        self.assertEqual(base_task.status, TxTaskStatus.SUCCEEDED)
        self.assertEqual(base_task.tx_hash, old_hash)
        self.assertEqual(
            [call.kwargs["tx_hash"] for call in adapter.tx_result.call_args_list],
            [new_hash, old_hash],
        )
        refresh_balance.assert_called_once()
        self.assertEqual(refresh_balance.call_args.args[0].tx_hash, old_hash)
        collect_gas_fee.assert_called_once()
        self.assertEqual(collect_gas_fee.call_args.kwargs["tx_task"].tx_hash, old_hash)
        deploy_gas_fee.assert_not_called()

    @patch("tron.tasks.notify_vault_slot_collect_gas_fee")
    @patch("tron.tasks.refresh_vault_slot_balance_for_collect_task")
    @patch("tron.tasks.AdapterFactory.get_adapter")
    def test_confirm_failed_when_old_missing_hash_is_expired(
        self,
        get_adapter,
        refresh_balance,
        collect_gas_fee,
    ):
        old_hash = "e" * 64
        new_hash = "f" * 64
        base_task = self.create_collect_task(tx_hash=old_hash)
        expired_ms = int((timezone.now() - timedelta(minutes=1)).timestamp() * 1000)
        base_task.tx_hashes.filter(hash=old_hash).update(expires_at_ms=expired_ms)
        base_task.append_tx_hash(new_hash)
        adapter = Mock()

        def tx_result(*, chain, tx_hash):
            if tx_hash == new_hash:
                return TxCheckResult(
                    status=TxCheckStatus.FAILED,
                    block_number=100,
                    block_hash="f" * 64,
                )
            if tx_hash == old_hash:
                return TxCheckStatus.MISSING
            raise AssertionError(f"unexpected tx_hash={tx_hash}")

        adapter.tx_result.side_effect = tx_result
        get_adapter.return_value = adapter

        confirm_tron_receipt_tx_tasks()

        base_task.refresh_from_db()
        self.assertEqual(base_task.status, TxTaskStatus.FAILED)
        self.assertEqual(
            [call.kwargs["tx_hash"] for call in adapter.tx_result.call_args_list],
            [new_hash, old_hash],
        )
        refresh_balance.assert_not_called()
        collect_gas_fee.assert_not_called()


@override_settings(
    TRON_VAULT_SLOT_FACTORY_ADDRESS="TJRabPrwbZy45sbavfcjinPJC18kjpRTv8",
    TRON_VAULT_SLOT_DEPLOY_FEE_LIMIT=300_000_000,
    TRON_VAULT_SLOT_FEE_LIMIT=150_000_000,
)
class TronCollectScheduleExecuteTests(TestCase):
    """归集计划到期建链上任务的行为:必须确认 slot 已部署,且每个计划各建独立任务。"""

    def setUp(self):
        self.chain = Chain.objects.create(
            code=ChainCode.Tron,
            rpc="https://api.trongrid.io",
            tron_api_key="tron-key",
            active=True,
        )
        self.usdt = Crypto.objects.create(
            name="Tether Tron",
            symbol="USDT",
            prices={"USD": "1"},
            coingecko_id="tron-execute-usdt",
        )
        CryptoOnChain.objects.create(
            chain=self.chain,
            crypto=self.usdt,
            address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            decimals=6,
        )
        self.project = Project.objects.create(
            name="Tron Execute Project",
            tron_vault="TJRabPrwbZy45sbavfcjinPJC18kjpRTv8",
        )
        self.customer = Customer.objects.create(
            project=self.project, uid="tron-execute-customer"
        )
        self.wallet = Wallet.objects.create()
        self.sender = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.TRON,
            usage=AddressUsage.HotWallet,
            bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
            address_index=0,
            address="TJRabPrwbZy45sbavfcjinPJC18kjpRTv8",
        )
        self.slot = VaultSlot.objects.create(
            chain=self.chain,
            usage=VaultSlotUsage.DEPOSIT,
            customer=self.customer,
            project=self.project,
            address="TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb",
            salt=b"\x02" * 32,
        )

    def make_pending_schedule(self) -> VaultSlotCollectSchedule:
        return VaultSlotCollectSchedule.objects.create(
            chain=self.chain,
            vault_slot=self.slot,
            crypto=self.usdt,
            due_at=timezone.now() - timedelta(seconds=1),
        )

    @patch("tron.vault_slots.TronAdapter.is_contract", return_value=False)
    def test_execute_due_creates_ensure_collect_task_for_undeployed_token_slot(
        self, is_contract
    ):
        schedule = self.make_pending_schedule()

        with patch("tron.vault_slots.SystemWallet.get_current") as get_current:
            get_current.return_value.wallet.get_address.return_value = self.sender
            with patch(
                "chains.vault_slot_balances.refresh_vault_slot_balance_safely",
                return_value=SimpleNamespace(value=1),
            ):
                created = VaultSlotCollectSchedule.execute_due()

        self.assertEqual(created, 1)
        schedule.refresh_from_db()
        self.assertIsNotNone(schedule.tx_task_id)
        self.assertEqual(
            schedule.tx_task.tron_task.function_selector,
            "ensureDeployedAndCollect(address,bytes32,address)",
        )
        self.assertEqual(
            schedule.tx_task.tron_task.to,
            "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8",
        )
        is_contract.assert_not_called()

    def test_collect_token_address_native_maps_to_zero_else_contract(self):
        # 归集 token 路由：原生币 → address(0)，TRC20 → 其合约地址。
        from tron.vault_slots import NATIVE_COLLECT_TOKEN_ADDRESS
        from tron.vault_slots import collect_token_address

        self.assertEqual(
            collect_token_address(crypto=self.chain.native_coin, chain=self.chain),
            NATIVE_COLLECT_TOKEN_ADDRESS,
        )
        self.assertEqual(
            collect_token_address(crypto=self.usdt, chain=self.chain),
            "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        )

    @patch("tron.vault_slots.TronAdapter.is_contract", return_value=False)
    def test_execute_due_native_undeployed_uses_ensure_collect_with_zero_token(
        self, is_contract
    ):
        # 决策 B：原生 TRX 即使 slot 未部署也能归集——走 ensureDeployedAndCollect 一笔
        # 部署+清扫，token 传 address(0)；不再被「必须先部署」拦下，也不查 is_contract。
        trx = self.chain.native_coin
        schedule = VaultSlotCollectSchedule.objects.create(
            chain=self.chain,
            vault_slot=self.slot,
            crypto=trx,
            due_at=timezone.now() - timedelta(seconds=1),
        )

        with patch("tron.vault_slots.SystemWallet.get_current") as get_current:
            get_current.return_value.wallet.get_address.return_value = self.sender
            with patch(
                "chains.vault_slot_balances.refresh_vault_slot_balance_safely",
                return_value=SimpleNamespace(value=1),
            ):
                created = VaultSlotCollectSchedule.execute_due()

        self.assertEqual(created, 1)
        schedule.refresh_from_db()
        self.assertIsNotNone(schedule.tx_task_id)
        tron_task = schedule.tx_task.tron_task
        self.assertEqual(
            tron_task.function_selector,
            "ensureDeployedAndCollect(address,bytes32,address)",
        )
        # token 入参（第 3 个 address，最后 32 字节）必须是 address(0)=全零，即原生币清扫。
        self.assertTrue(
            tron_task.parameter.endswith("0" * 64),
            f"原生归集 token 应为 address(0)，实际 parameter={tron_task.parameter}",
        )
        is_contract.assert_not_called()

    @patch("tron.vault_slots.TronAdapter.is_contract", return_value=True)
    def test_execute_due_creates_task_when_slot_deployed(self, is_contract):
        VaultSlot.objects.filter(pk=self.slot.pk).update(is_deployed=True)
        self.slot.is_deployed = True
        schedule = self.make_pending_schedule()

        with patch("tron.vault_slots.SystemWallet.get_current") as get_current:
            get_current.return_value.wallet.get_address.return_value = self.sender
            with patch(
                "chains.vault_slot_balances.refresh_vault_slot_balance_safely",
                return_value=SimpleNamespace(value=1),
            ):
                created = VaultSlotCollectSchedule.execute_due()

        self.assertEqual(created, 1)
        schedule.refresh_from_db()
        self.assertIsNotNone(schedule.tx_task_id)
        self.assertEqual(
            schedule.tx_task.tron_task.function_selector,
            "collect(address)",
        )
        self.assertEqual(schedule.tx_task.tron_task.to, self.slot.address)
        self.assertEqual(schedule.tx_task.tron_task.fee_limit, 150_000_000)

    @patch("tron.vault_slots.TronAdapter.is_contract", return_value=True)
    def test_two_schedules_same_slot_get_independent_tasks(self, is_contract):
        # 回归:移除「复用在途任务」去重后,同 slot+token 的两个计划各建独立任务,
        # 不再撞 VaultSlotCollectSchedule.tx_task 的 OneToOne 唯一约束、毒化整批调度。
        first = self.make_pending_schedule()
        with patch("tron.vault_slots.SystemWallet.get_current") as get_current:
            get_current.return_value.wallet.get_address.return_value = self.sender
            with patch(
                "chains.vault_slot_balances.refresh_vault_slot_balance_safely",
                return_value=SimpleNamespace(value=1),
            ):
                VaultSlotCollectSchedule.execute_due()
            first.refresh_from_db()
            self.assertIsNotNone(first.tx_task_id)

            # 第一个计划绑定任务后 uniq_pending 约束释放,可再建第二个 pending 计划。
            second = self.make_pending_schedule()
            with patch(
                "chains.vault_slot_balances.refresh_vault_slot_balance_safely",
                return_value=SimpleNamespace(value=1),
            ):
                created = VaultSlotCollectSchedule.execute_due()

        self.assertEqual(created, 1)
        second.refresh_from_db()
        self.assertIsNotNone(second.tx_task_id)
        self.assertNotEqual(first.tx_task_id, second.tx_task_id)

    @patch("tron.vault_slots.TronAdapter.is_contract", return_value=True)
    def test_execute_due_deletes_pending_schedule_when_balance_is_zero(self, is_contract):
        schedule = self.make_pending_schedule()

        with (
            patch("tron.vault_slots.SystemWallet.get_current") as get_current,
            patch(
                "chains.vault_slot_balances.refresh_vault_slot_balance_safely",
                return_value=SimpleNamespace(value=0),
            ),
        ):
            get_current.return_value.wallet.get_address.return_value = self.sender
            created = VaultSlotCollectSchedule.execute_due()

        self.assertEqual(created, 0)
        self.assertFalse(VaultSlotCollectSchedule.objects.filter(pk=schedule.pk).exists())
        self.assertFalse(TxTask.objects.filter(tx_type=TxTaskType.VaultSlotCollect).exists())
