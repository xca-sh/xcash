from __future__ import annotations

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.test import override_settings
from web3 import Web3

from chains.models import Address
from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from chains.models import Wallet
from core.models import SYSTEM_SETTINGS_CACHE_KEY
from evm.models import EvmScanCursor


@override_settings(DEBUG=False)
class EvmReconcileBlocksTests(TestCase):
    """reconcile_blocks 必须能复用主扫描的产出通路且不污染 cursor。"""

    def setUp(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        self.native = crypto_create("Ether Recon Scan", "ETHRS", "ethereum-recon-scan")
        self.chain = Chain.objects.create(
            code="eth-recon-scan",
            name="Ether Recon Scan",
            type=ChainType.EVM,
            chain_id=90_002,
            rpc="http://eth-recon-scan.local",
            native_coin=self.native,
            confirm_block_count=6,
            active=True,
            latest_block_number=500,
        )
        self.wallet = Wallet.objects.create()
        self.addr = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000b1"
            ),
        )
        # 固定 cursor，断言复扫前后不发生推进。
        self.cursor = EvmScanCursor.objects.create(
            chain=self.chain,
            last_scanned_block=100,
        )

    def tearDown(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        super().tearDown()

    @patch(
        "evm.scanner.service.EvmLogScanner.scan_range",
    )
    def test_reconcile_sparse_blocks_scans_contiguous_segments(
        self,
        erc20_scan_mock,
    ):
        # 多个 stale 任务命中相距很远的块时，对账只能扫命中的连续块段，
        # 不能扩成 [min..max] 巨大区间拖垮 RPC。
        from evm.scanner.service import EvmScannerService

        erc20_scan_mock.side_effect = [None, None, None]

        result = EvmScannerService.reconcile_blocks(
            chain=self.chain,
            block_numbers={10, 11, 500, 501, 900},
        )

        erc20_ranges = [
            (call.kwargs["from_block"], call.kwargs["to_block"])
            for call in erc20_scan_mock.call_args_list
        ]
        self.assertEqual(erc20_ranges, [(10, 11), (500, 501), (900, 900)])
        self.assertEqual(result.from_block, 10)
        self.assertEqual(result.to_block, 900)

    @patch(
        "evm.scanner.service.EvmLogScanner.scan_range",
    )
    def test_reconcile_skips_erc20_scan_when_cursor_disabled(
        self,
        erc20_scan_mock,
    ):
        from evm.scanner.service import EvmScannerService

        self.cursor.enabled = False
        self.cursor.save(update_fields=["enabled"])
        erc20_scan_mock.return_value = None

        result = EvmScannerService.reconcile_blocks(
            chain=self.chain,
            block_numbers={10},
        )

        erc20_scan_mock.assert_not_called()


def crypto_create(name: str, symbol: str, coingecko_id: str):
    """惰性引用 Crypto，避免模块导入阶段强依赖 currencies。

    当前项目里 currencies.Crypto 位于 currencies.models；测试用例小工具这里集中处理，
    保持各用例 setUp 代码更聚焦。
    """
    from currencies.models import Crypto

    return Crypto.objects.create(name=name, symbol=symbol, coingecko_id=coingecko_id)
