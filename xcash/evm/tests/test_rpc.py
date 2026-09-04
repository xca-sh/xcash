from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from celery.exceptions import SoftTimeLimitExceeded
from django.test import SimpleTestCase
from django.test import TestCase
from web3 import Web3
from web3.exceptions import ExtraDataLengthError

from chains.constants import ChainCode
from chains.models import Chain
from currencies.models import Crypto
from evm.scanner import rpc as rpc_module
from evm.scanner.rpc import EvmScannerRpcClient
from evm.scanner.rpc import EvmScannerRpcError
from evm.tests._fixtures import make_evm_chain


@patch.object(rpc_module, "_EVM_RPC_RETRY_BACKOFF_SECONDS", (0, 0))
class EvmScannerRpcErrorMessageTests(SimpleTestCase):
    def test_get_logs_error_includes_rpc_method_and_raw_reason(self):
        # 游标 last_error 直接使用此异常文本；必须带上具体 RPC 方法和节点原始报错，
        # 否则后台只能看到失败区块范围，无法判断是套餐限流、超时还是节点内部错误。
        chain = SimpleNamespace(
            code="bsc-mainnet",
            evm_log_max_block_range=10,
            w3=SimpleNamespace(
                eth=SimpleNamespace(
                    get_logs=Mock(
                        side_effect=ValueError(
                            {"code": -32005, "message": "limit exceeded: 5000 results"}
                        )
                    )
                )
            ),
        )

        with self.assertRaises(EvmScannerRpcError) as caught:
            EvmScannerRpcClient(chain=chain).get_logs(
                from_block=100,
                to_block=109,
                addresses=[
                    Web3.to_checksum_address(
                        "0x00000000000000000000000000000000000000aa"
                    )
                ],
                topic0=Web3.to_hex(
                    Web3.keccak(text="Transfer(address,address,uint256)")
                ),
                summary="获取 EVM 日志失败",
            )

        message = str(caught.exception)
        self.assertIn("获取 EVM 日志失败", message)
        self.assertIn("rpc=eth_getLogs", message)
        self.assertIn("limit exceeded: 5000 results", message)
        self.assertLess(message.index("rpc=eth_getLogs"), message.index("from=100"))
        self.assertIn("rpc=eth_getLogs", message[:60])

    def test_get_full_block_error_includes_rpc_method_and_raw_reason(self):
        chain = SimpleNamespace(
            code="bsc-mainnet",
            w3=SimpleNamespace(
                eth=SimpleNamespace(
                    get_block=Mock(side_effect=TimeoutError("read timeout"))
                )
            ),
        )

        with self.assertRaises(EvmScannerRpcError) as caught:
            EvmScannerRpcClient(chain=chain).get_full_block(block_number=9_586_911)

        message = str(caught.exception)
        self.assertIn("获取完整区块失败", message)
        self.assertIn("rpc=eth_getBlockByNumber", message)
        self.assertIn("read timeout", message)
        self.assertLess(
            message.index("rpc=eth_getBlockByNumber"),
            message.index("block=9586911"),
        )
        self.assertIn("rpc=eth_getBlockByNumber", message[:60])

    def test_get_logs_error_preserves_full_raw_reason(self):
        # 节点错误文本会直接进入扫描游标；长错误不能被提前截断，否则后台无法看到
        # 供应商返回的完整限制参数、建议区间或请求上下文。
        raw_reason = "limit exceeded: " + "x" * 360
        chain = SimpleNamespace(
            code="arbitrum-mainnet",
            evm_log_max_block_range=10,
            w3=SimpleNamespace(
                eth=SimpleNamespace(
                    get_logs=Mock(
                        side_effect=ValueError(
                            {
                                "code": -32005,
                                "message": raw_reason,
                            }
                        )
                    )
                )
            ),
        )

        with self.assertRaises(EvmScannerRpcError) as caught:
            EvmScannerRpcClient(chain=chain).get_logs(
                from_block=45_996_974,
                to_block=45_996_983,
                addresses=[
                    Web3.to_checksum_address(
                        "0x00000000000000000000000000000000000000aa"
                    )
                ],
                topic0=Web3.to_hex(
                    Web3.keccak(text="Transfer(address,address,uint256)")
                ),
                summary="获取 EVM 日志失败",
            )

        message = str(caught.exception)
        self.assertIn(raw_reason, message)
        self.assertIn("from=45996974 to=45996983", message)


class EvmScannerRpcClientTests(TestCase):
    def setUp(self):
        self.native = Crypto.objects.create(
            name="BNB RPC",
            symbol="BNBR",
            coingecko_id="binancecoin-rpc",
        )
        self.chain = make_evm_chain(
            code=ChainCode.Ethereum,
            rpc="http://bsc.rpc.local",
        )

    def test_get_logs_splits_request_by_chain_max_block_range(self):
        # RPC 供应商限制 eth_getLogs 区块跨度时，应按链配置切片并聚合结果。
        Chain.objects.filter(pk=self.chain.pk).update(evm_log_max_block_range=10)
        self.chain.refresh_from_db()
        requested_ranges: list[tuple[int, int]] = []

        def fake_get_logs(filter_params: dict) -> list[dict]:
            requested_ranges.append(
                (filter_params["fromBlock"], filter_params["toBlock"])
            )
            return [
                {
                    "blockNumber": filter_params["fromBlock"],
                    "logIndex": 0,
                    "transactionHash": bytes.fromhex("ab" * 32),
                }
            ]

        self.chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(get_logs=Mock(side_effect=fake_get_logs))
        )

        logs = EvmScannerRpcClient(chain=self.chain).get_logs(
            from_block=100,
            to_block=124,
            addresses=[
                Web3.to_checksum_address("0x00000000000000000000000000000000000000aa")
            ],
            topic0=Web3.to_hex(Web3.keccak(text="Transfer(address,address,uint256)")),
        )

        self.assertEqual(requested_ranges, [(100, 109), (110, 119), (120, 124)])
        self.assertEqual(len(logs), 3)

    def test_get_logs_bisects_range_on_result_too_large_error(self):
        # 结果集超限是确定性错误：应二分区块区间下探，而非重试后卡死游标导致漏账。
        Chain.objects.filter(pk=self.chain.pk).update(evm_log_max_block_range=10)
        self.chain.refresh_from_db()
        succeeded_ranges: list[tuple[int, int]] = []

        def fake_get_logs(filter_params: dict) -> list[dict]:
            lo, hi = filter_params["fromBlock"], filter_params["toBlock"]
            # 跨度 > 2 块的查询模拟命中节点结果上限，需要继续二分。
            if hi - lo > 2:
                raise ValueError("query returned more than 10000 results")
            succeeded_ranges.append((lo, hi))
            return [
                {
                    "blockNumber": lo,
                    "logIndex": 0,
                    "transactionHash": bytes.fromhex("ab" * 32),
                }
            ]

        self.chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(get_logs=Mock(side_effect=fake_get_logs))
        )

        logs = EvmScannerRpcClient(chain=self.chain).get_logs(
            from_block=100,
            to_block=109,
            addresses=[
                Web3.to_checksum_address("0x00000000000000000000000000000000000000aa")
            ],
            topic0=Web3.to_hex(Web3.keccak(text="Transfer(address,address,uint256)")),
        )

        # 二分后每个成功子区间跨度都 <= 2，且完整覆盖 [100, 109] 无缺口。
        self.assertTrue(all(hi - lo <= 2 for lo, hi in succeeded_ranges))
        covered = sorted(b for lo, hi in succeeded_ranges for b in range(lo, hi + 1))
        self.assertEqual(covered, list(range(100, 110)))
        self.assertEqual(len(logs), len(succeeded_ranges))

    def test_get_logs_single_block_still_too_large_raises(self):
        # 二分到单块仍超限时必须上抛告警，绝不静默跳过（跳过等于永久漏账）。
        self.chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                get_logs=Mock(
                    side_effect=ValueError("query returned more than 10000 results")
                )
            )
        )

        with self.assertRaises(EvmScannerRpcError):
            EvmScannerRpcClient(chain=self.chain).get_logs(
                from_block=100,
                to_block=100,
                addresses=[
                    Web3.to_checksum_address(
                        "0x00000000000000000000000000000000000000aa"
                    )
                ],
                topic0=Web3.to_hex(
                    Web3.keccak(text="Transfer(address,address,uint256)")
                ),
            )

    def test_get_logs_accepts_multiple_topic0_values(self):
        # native deposit + ERC20 Transfer 统一扫描时，topic0 第一位需要用 OR 查询。
        captured_filters: list[dict] = []

        def fake_get_logs(filter_params: dict) -> list[dict]:
            captured_filters.append(filter_params)
            return []

        self.chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(get_logs=Mock(side_effect=fake_get_logs))
        )
        topics = [
            Web3.to_hex(Web3.keccak(text="XcashNativeReceived(address,uint256)")),
            Web3.to_hex(Web3.keccak(text="Transfer(address,address,uint256)")),
        ]

        EvmScannerRpcClient(chain=self.chain).get_logs(
            from_block=100,
            to_block=100,
            addresses=[
                Web3.to_checksum_address("0x00000000000000000000000000000000000000aa")
            ],
            topic0=topics,
        )

        self.assertEqual(captured_filters[0]["topics"], [topics])

    def test_get_logs_omits_address_filter_when_addresses_is_none(self):
        # VaultSlot 自定义事件按 topic 全链查询，不能把海量 slot 地址塞进 address 条件。
        captured_filters: list[dict] = []

        def fake_get_logs(filter_params: dict) -> list[dict]:
            captured_filters.append(filter_params)
            return []

        self.chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(get_logs=Mock(side_effect=fake_get_logs))
        )

        EvmScannerRpcClient(chain=self.chain).get_logs(
            from_block=100,
            to_block=100,
            addresses=None,
            topic0=Web3.to_hex(
                Web3.keccak(text="XcashNativeReceived(address,uint256)")
            ),
        )

        self.assertNotIn("address", captured_filters[0])

    @patch("evm.scanner.rpc.EvmScannerRpcClient._build_poa_retry_w3")
    def test_get_block_timestamp_retries_with_poa_when_extradata_is_too_long(
        self,
        build_poa_retry_w3_mock,
    ):
        # 遇到 POA extraData 校验错误时应自动用 POA middleware 重建 w3 重试一次。
        # is_poa 现由链常量推导且只读，POA 重试不再回写 DB，故仅验证重试路径被触发。
        failing_w3 = SimpleNamespace(
            eth=SimpleNamespace(
                get_block=Mock(
                    side_effect=ExtraDataLengthError(
                        "poa extraData too long",
                    )
                )
            )
        )
        retry_w3 = SimpleNamespace(
            eth=SimpleNamespace(
                get_block=Mock(return_value={"timestamp": 1_776_734_136})
            )
        )
        self.chain.__dict__["w3"] = failing_w3
        build_poa_retry_w3_mock.return_value = retry_w3

        timestamp = EvmScannerRpcClient(chain=self.chain).get_block_timestamp(
            block_number=93_739_122
        )

        self.assertEqual(timestamp, 1_776_734_136)
        build_poa_retry_w3_mock.assert_called_once()

    @patch.object(rpc_module, "_EVM_RPC_RETRY_BACKOFF_SECONDS", (0, 0))
    def test_get_full_block_retries_until_success_within_attempt_budget(self):
        # 瞬时网络抖动应被重试吸收：第三次成功就当作整体成功，不应上抛 EvmScannerRpcError。
        get_block_mock = Mock(
            side_effect=[
                TimeoutError("read timeout"),
                TimeoutError("read timeout"),
                {"number": 100, "transactions": []},
            ]
        )
        self.chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(get_block=get_block_mock)
        )

        block = EvmScannerRpcClient(chain=self.chain).get_full_block(block_number=100)

        self.assertEqual(block["number"], 100)
        self.assertEqual(get_block_mock.call_count, 3)

    @patch.object(rpc_module, "_EVM_RPC_RETRY_BACKOFF_SECONDS", (0, 0))
    def test_get_full_block_exhausts_retries_and_wraps_as_rpc_error(self):
        # 退避窗口耗尽后才包装成 EvmScannerRpcError 上抛；游标据此记录 last_error。
        get_block_mock = Mock(side_effect=TimeoutError("read timeout"))
        self.chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(get_block=get_block_mock)
        )

        with self.assertRaises(EvmScannerRpcError):
            EvmScannerRpcClient(chain=self.chain).get_full_block(block_number=100)

        self.assertEqual(get_block_mock.call_count, 3)

    def test_get_latest_block_number_caches_result_within_single_client(self):
        # 单 tick 内统一日志扫描和兜底复扫复用同一 client 时，eth_blockNumber 只应打一次。
        chain = SimpleNamespace(
            code="bsc-mainnet",
            get_latest_block_number=99,
        )
        client = EvmScannerRpcClient(chain=chain)

        first = client.get_latest_block_number()
        second = client.get_latest_block_number()

        self.assertEqual(first, 99)
        self.assertEqual(second, 99)
        # 第二次调用读自实例缓存，不应再触达底层 Web3 / chain property。
        self.assertIsNotNone(client._cached_latest_block)

    @patch("evm.scanner.rpc.EvmScannerRpcClient._build_poa_retry_w3")
    def test_get_full_block_retries_with_poa_when_extradata_is_too_long(
        self,
        build_poa_retry_w3_mock,
    ):
        # is_poa 现由链常量推导且只读，POA 重试不再回写 DB，故仅验证重试路径被触发。
        failing_w3 = SimpleNamespace(
            eth=SimpleNamespace(
                get_block=Mock(
                    side_effect=ExtraDataLengthError(
                        "poa extraData too long",
                    )
                )
            )
        )
        retry_w3 = SimpleNamespace(
            eth=SimpleNamespace(
                get_block=Mock(return_value={"number": 93_739_122, "transactions": []})
            )
        )
        self.chain.__dict__["w3"] = failing_w3
        build_poa_retry_w3_mock.return_value = retry_w3

        block = EvmScannerRpcClient(chain=self.chain).get_full_block(
            block_number=93_739_122
        )

        self.assertEqual(block["number"], 93_739_122)
        build_poa_retry_w3_mock.assert_called_once()


class EvmScannerSoftTimeLimitTests(SimpleTestCase):
    """Celery 软超时必须原样穿透 RPC 重试层。

    SoftTimeLimitExceeded 继承自 Exception，一旦被 _call_with_retry 当成普通 RPC
    故障，就会走"退避 sleep → 重试 → 包成 EvmScannerRpcError"这条路；而调用方
    _scan_evm_chain 对 EvmScannerRpcError 只打 warning 并继续跑确认调度。结果是
    已达 40s 软限制的任务被拖到 50s 硬超时（SIGKILL），finally 里的 mark_scanned
    与本段 chunk 游标提交都来不及执行，还连带触发 prefork 子进程重建。
    """

    def make_chain(self, *, get_logs):
        return SimpleNamespace(
            code="bsc-mainnet",
            evm_log_max_block_range=10,
            w3=SimpleNamespace(eth=SimpleNamespace(get_logs=get_logs)),
        )

    def call_get_logs(self, chain):
        return EvmScannerRpcClient(chain=chain).get_logs(
            from_block=100,
            to_block=109,
            addresses=[
                Web3.to_checksum_address("0x00000000000000000000000000000000000000aa")
            ],
            topic0=Web3.to_hex(Web3.keccak(text="Transfer(address,address,uint256)")),
            summary="获取 EVM 日志失败",
        )

    def test_soft_time_limit_propagates_without_retry_or_backoff(self):
        # 断言 SoftTimeLimitExceeded 本身上抛，即已证明它没有被包装成
        # EvmScannerRpcError（后者是 RuntimeError 子类，类型不匹配会让断言失败）。
        get_logs = Mock(side_effect=SoftTimeLimitExceeded())
        chain = self.make_chain(get_logs=get_logs)

        with (
            patch.object(rpc_module.time, "sleep") as sleep_mock,
            self.assertRaises(SoftTimeLimitExceeded),
        ):
            self.call_get_logs(chain)

        # 只尝试一次：软超时后再发 RPC 只会继续消耗剩余的 10s 硬超时预算。
        self.assertEqual(get_logs.call_count, 1)
        sleep_mock.assert_not_called()

    def test_ordinary_rpc_error_still_retries_and_wraps(self):
        # 反向用例：确认穿透分支没有顺带关掉普通 RPC 故障的重试与统一包装。
        get_logs = Mock(side_effect=TimeoutError("read timeout"))
        chain = self.make_chain(get_logs=get_logs)

        with (
            patch.object(rpc_module, "_EVM_RPC_RETRY_BACKOFF_SECONDS", (0, 0)),
            self.assertRaises(EvmScannerRpcError),
        ):
            self.call_get_logs(chain)

        self.assertEqual(get_logs.call_count, 3)
