import os
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from django.test import SimpleTestCase
from django.test import override_settings
from tron.client import TronHttpClient

from chains.constants import ChainCode
from common import http_client


class SharedHttpClientTests(SimpleTestCase):
    def tearDown(self):
        http_client._client = None
        http_client._client_pid = None
        super().tearDown()

    def test_shared_client_is_reused_within_process(self):
        first = http_client.shared_client()

        self.assertIs(http_client.shared_client(), first)

    def test_shared_client_is_rebuilt_after_fork(self):
        # prefork 子进程继承了父进程的模块状态：pid 变化后必须新建 Client，
        # 不能复用父进程打开的连接（socket 跨进程共享会串包）。
        parent_client = http_client.shared_client()

        with patch.object(http_client.os, "getpid", return_value=os.getpid() + 1):
            child_client = http_client.shared_client()

        self.assertIsNot(child_client, parent_client)


@override_settings(TRON_RPC_TIMEOUT=3.0)
class TronHttpClientSharedConnectionTests(SimpleTestCase):
    def setUp(self):
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(
                200,
                json={"block_header": {"raw_data": {"number": 1234}}},
            )

        transport_client = httpx.Client(transport=httpx.MockTransport(handler))
        patcher = patch.multiple(
            http_client,
            _client=transport_client,
            _client_pid=os.getpid(),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(transport_client.close)

    def test_requests_from_separate_clients_share_one_http_client(self):
        # 每个扫描 tick 都会新建 TronHttpClient；真正发请求的必须是进程共享 Client，
        # 鉴权头仍逐请求带上。
        chain = SimpleNamespace(code=ChainCode.Tron, tron_api_key="tron-key")

        first = TronHttpClient(chain=chain).get_latest_solid_block_number()
        second = TronHttpClient(chain=chain).get_latest_solid_block_number()

        self.assertEqual((first, second), (1234, 1234))
        self.assertEqual(len(self.requests), 2)
        for request in self.requests:
            self.assertEqual(request.headers["TRON-PRO-API-KEY"], "tron-key")
            self.assertEqual(request.extensions["timeout"]["read"], 3.0)
