from unittest.mock import Mock
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.test import override_settings
from web3 import Web3

from chains.constants import ChainCode
from chains.models import Address
from chains.models import AddressUsage
from chains.models import ChainType
from chains.models import VaultSlot
from chains.models import VaultSlotUsage
from chains.models import Wallet
from currencies.models import Crypto
from currencies.models import CryptoOnChain
from evm.scanner.watchers import ADDRESS_LOOKUP_CHUNK_SIZE
from evm.scanner.watchers import clear_token_registry_cache
from evm.scanner.watchers import load_owned_addresses_for_candidates
from evm.scanner.watchers import load_token_registry
from evm.tests._fixtures import make_evm_chain
from evm.vault_slots import predict_address
from invoices.models import DifferRecipientAddress
from projects.models import Customer
from projects.models import Project

WATCHER_TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "evm-token-registry-tests",
    }
}


@override_settings(CACHES=WATCHER_TEST_CACHES)
class EvmTokenRegistryCacheTests(TestCase):
    def setUp(self):
        cache.clear()
        self.native = Crypto.objects.create(
            name="Watcher Native",
            symbol="WNATIVE",
            coingecko_id="watcher-native",
        )
        self.chain = make_evm_chain(
            code=ChainCode.Ethereum,
            rpc="http://watcher.local",
        )
        self.token = Crypto.objects.create(
            name="Watcher Token",
            symbol="WTKN",
            coingecko_id="watcher-token",
        )
        self.token_on_chain = CryptoOnChain.objects.create(
            crypto=self.token,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000aa"
            ),
            decimals=18,
        )
        self.wallet = Wallet.objects.create()
        self.address = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=0,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000bb"
            ),
        )
        self.project = self._create_project()
        self.customer = Customer.objects.create(
            project=self.project,
            uid="watcher-customer",
        )
        self.vault_slot = VaultSlot.objects.create(
            customer=self.customer,
            usage=VaultSlotUsage.DEPOSIT,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000bc"
            ),
            salt=b"\x01" * 32,
        )

    def tearDown(self):
        clear_token_registry_cache()
        cache.clear()

    def test_load_token_registry_returns_supported_tokens(self):
        token_registry = load_token_registry(chain=self.chain, refresh=True)

        self.assertEqual(
            token_registry,
            {self.token_on_chain.address: self.token_on_chain},
        )

    def test_candidate_lookup_matches_vault_slots(self):
        unknown_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000d02"
        )

        owned_addresses = load_owned_addresses_for_candidates(
            chain=self.chain,
            addresses={self.vault_slot.address, unknown_address},
        )

        self.assertEqual(
            owned_addresses,
            frozenset({self.vault_slot.address}),
        )

    def test_candidate_lookup_matches_differ_recipient_addresses(self):
        differ_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000d04"
        )
        DifferRecipientAddress.objects.create(
            project=self.project,
            chain_type=ChainType.EVM,
            address=differ_address,
        )

        owned_addresses = load_owned_addresses_for_candidates(
            chain=self.chain,
            addresses={differ_address},
        )

        self.assertEqual(owned_addresses, frozenset({differ_address}))

    def test_candidate_lookup_excludes_non_candidate_addresses(self):
        owned_addresses = load_owned_addresses_for_candidates(
            chain=self.chain,
            addresses={self.address.address},
        )

        self.assertEqual(owned_addresses, frozenset())

    def test_candidate_lookup_scopes_vault_slots_to_chain(self):
        other_chain = make_evm_chain(
            code=ChainCode.BSC,
            rpc="http://watcher-other.local",
        )
        other_customer = Customer.objects.create(
            project=self.project,
            uid="watcher-customer-other-chain",
        )
        other_slot_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000d03"
        )
        VaultSlot.objects.create(
            customer=other_customer,
            usage=VaultSlotUsage.DEPOSIT,
            chain=other_chain,
            address=other_slot_address,
            salt=b"\x03" * 32,
        )

        owned_addresses = load_owned_addresses_for_candidates(
            chain=self.chain,
            addresses={self.vault_slot.address, other_slot_address},
        )

        self.assertEqual(owned_addresses, frozenset({self.vault_slot.address}))

    def test_crypto_on_chain_save_refreshes_cached_token_set_after_commit(self):
        load_token_registry(chain=self.chain, refresh=True)
        new_token = Crypto.objects.create(
            name="Watcher Token Two",
            symbol="WTKN2",
            coingecko_id="watcher-token-two",
        )
        token_address = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000ee"
        )

        with self.captureOnCommitCallbacks(execute=True):
            CryptoOnChain.objects.create(
                crypto=new_token,
                chain=self.chain,
                address=token_address,
                decimals=6,
            )

        token_registry = load_token_registry(chain=self.chain)
        self.assertIn(token_address, token_registry)

    def test_crypto_on_chain_delete_refreshes_cached_token_set_after_commit(self):
        initial_registry = load_token_registry(chain=self.chain, refresh=True)
        self.assertIn(self.token_on_chain.address, initial_registry)

        with self.captureOnCommitCallbacks(execute=True):
            self.token_on_chain.delete()

        token_registry = load_token_registry(chain=self.chain)
        self.assertNotIn(self.token_on_chain.address, token_registry)

    def test_crypto_active_change_refreshes_cached_token_set_after_commit(self):
        initial_registry = load_token_registry(chain=self.chain, refresh=True)
        self.assertIn(self.token_on_chain.address, initial_registry)

        with self.captureOnCommitCallbacks(execute=True):
            self.token.active = False
            self.token.save(update_fields=["active"])

        token_registry = load_token_registry(chain=self.chain)
        self.assertNotIn(self.token_on_chain.address, token_registry)

    def test_crypto_price_only_save_keeps_cached_token_set(self):
        # 价格刷新每 60 秒对每个币执行 save(update_fields=["prices"])，扫描链路并不读取
        # crypto.prices，不得因此清空重建代币表缓存。
        # 用 queryset.update 绕开信号把 DB 改脏：缓存一旦被重建就会看到新内容，
        # 断言缓存仍是旧内容即证明本次保存没有触发失效。
        load_token_registry(chain=self.chain, refresh=True)
        Crypto.objects.filter(pk=self.token.pk).update(active=False)

        with self.captureOnCommitCallbacks(execute=True):
            self.token.prices = {"USD": "1.0001"}
            self.token.save(update_fields=["prices"])

        token_registry = load_token_registry(chain=self.chain)
        self.assertIn(self.token_on_chain.address, token_registry)

    @patch("currencies.tasks.http_client.get")
    def test_price_refresh_task_keeps_cached_token_set(self, mock_get):
        # 直接跑真实的行情刷新任务：任务写入的 update_fields 一旦超出豁免集（曾漏掉
        # prices_updated_at），每分钟都会把所有 EVM 链的代币表缓存清空重建。
        from currencies.models import Fiat
        from currencies.tasks import refresh_crypto_prices

        Fiat.objects.get_or_create(code="USD")
        mock_get.return_value = Mock(
            raise_for_status=Mock(),
            json=Mock(return_value={"watcher-token": {"usd": 1.0001}}),
        )
        load_token_registry(chain=self.chain, refresh=True)
        # 绕开信号把 DB 改脏：缓存一旦被重建就会读到新精度。
        CryptoOnChain.objects.filter(pk=self.token_on_chain.pk).update(decimals=6)

        with self.captureOnCommitCallbacks(execute=True):
            refresh_crypto_prices()

        self.token.refresh_from_db()
        self.assertIsNotNone(self.token.prices_updated_at)
        token_registry = load_token_registry(chain=self.chain)
        self.assertEqual(token_registry[self.token_on_chain.address].decimals, 18)

    def test_crypto_full_save_refreshes_cached_token_set_after_commit(self):
        # 全量 save 拿不到 update_fields，无法判断改了什么，必须按“可能相关”照常失效，
        # 否则扫描器会长期拿着过期的 Crypto。
        load_token_registry(chain=self.chain, refresh=True)
        Crypto.objects.filter(pk=self.token.pk).update(active=False)

        with self.captureOnCommitCallbacks(execute=True):
            self.token.refresh_from_db()
            self.token.save()

        token_registry = load_token_registry(chain=self.chain)
        self.assertNotIn(self.token_on_chain.address, token_registry)

    def test_candidate_lookup_matches_owned_addresses_across_chunks(self):
        # 候选集跨多个分片时，各来源（VaultSlot / DifferRecipientAddress）的命中结果
        # 必须完整合并，不能只剩某一片的结果。
        vault_slot_addresses = set()
        for index in range(3):
            customer = Customer.objects.create(
                project=self.project,
                uid=f"watcher-chunk-customer-{index}",
            )
            address = Web3.to_checksum_address(f"0x{0x3000 + index:040x}")
            VaultSlot.objects.create(
                customer=customer,
                usage=VaultSlotUsage.DEPOSIT,
                chain=self.chain,
                address=address,
                salt=bytes([index + 1]) * 32,
            )
            vault_slot_addresses.add(address)

        differ_addresses = set()
        for index in range(2):
            address = Web3.to_checksum_address(f"0x{0x3100 + index:040x}")
            DifferRecipientAddress.objects.create(
                project=self.project,
                chain_type=ChainType.EVM,
                address=address,
            )
            differ_addresses.add(address)

        unknown_addresses = {
            Web3.to_checksum_address(f"0x{0x3200 + index:040x}") for index in range(4)
        }
        candidates = vault_slot_addresses | differ_addresses | unknown_addresses

        unchunked = load_owned_addresses_for_candidates(
            chain=self.chain,
            addresses=candidates,
        )
        # 分片尺寸压到 2，强制切成多片，结果必须与一次性查询完全一致。
        with patch("evm.scanner.watchers.ADDRESS_LOOKUP_CHUNK_SIZE", 2):
            chunked = load_owned_addresses_for_candidates(
                chain=self.chain,
                addresses=candidates,
            )

        expected = frozenset(vault_slot_addresses | differ_addresses)
        self.assertEqual(unchunked, expected)
        self.assertEqual(chunked, expected)

    def test_candidate_lookup_handles_candidate_set_larger_than_chunk_size(self):
        # 单个日志窗口可抽出上万个候选地址，超过分片尺寸时必须照常返回正确结果，
        # 而不是把超大 address__in 直接下发给 DB。
        candidates = {
            Web3.to_checksum_address(f"0x{0x4000 + index:040x}")
            for index in range(ADDRESS_LOOKUP_CHUNK_SIZE * 2 + 1)
        }
        candidates.add(self.vault_slot.address)

        owned_addresses = load_owned_addresses_for_candidates(
            chain=self.chain,
            addresses=candidates,
        )

        self.assertEqual(owned_addresses, frozenset({self.vault_slot.address}))

    def _create_project(self) -> Project:
        suffix = Project.objects.count()
        return Project.objects.create(
            name=f"watcher-project-{suffix}",
            webhook="https://example.com/webhook",
        )


@override_settings(CACHES=WATCHER_TEST_CACHES)
class CrossChainDepositSlotTests(TestCase):
    """充值地址跨 EVM 链共用时，扫描应能识别并按需补建本链 VaultSlot。"""

    def setUp(self):
        cache.clear()
        self.eth = make_evm_chain(code=ChainCode.Ethereum, rpc="http://eth.local")
        self.bsc = make_evm_chain(code=ChainCode.BSC, rpc="http://bsc.local")
        self.vault_address = Web3.to_checksum_address("0x" + "11" * 20)
        self.project = Project.objects.create(
            name="xchain-project",
            webhook="https://example.com/webhook",
            evm_vault=self.vault_address,
        )
        self.customer = Customer.objects.create(
            project=self.project,
            uid="xchain-customer",
        )
        self.salt = VaultSlot.build_salt(
            usage=VaultSlotUsage.DEPOSIT,
            customer=self.customer,
        )
        # 充值地址跨 EVM 链确定相同：用任一 EVM 链预测即可。
        self.deposit_address = predict_address(
            chain=self.eth,
            vault=self.vault_address,
            salt=self.salt,
        )
        # 客户只在 Ethereum 取过充值地址：仅有 ETH 行、无 BSC 行。
        self.eth_slot = VaultSlot.objects.create(
            customer=self.customer,
            usage=VaultSlotUsage.DEPOSIT,
            chain=self.eth,
            address=self.deposit_address,
            salt=self.salt,
        )

    def tearDown(self):
        cache.clear()

    def test_cross_chain_deposit_is_recognized_and_slot_materialized(self):
        owned = load_owned_addresses_for_candidates(
            chain=self.bsc,
            addresses={self.deposit_address},
        )

        self.assertEqual(owned, frozenset({self.deposit_address}))
        slot = VaultSlot.objects.get(
            chain=self.bsc,
            customer=self.customer,
            usage=VaultSlotUsage.DEPOSIT,
        )
        self.assertEqual(slot.address, self.deposit_address)
        self.assertEqual(bytes(slot.salt), self.salt)
        # ERC20 充值不预部署：补建只落行，部署留给归集前置闸门按需触发。
        self.assertFalse(slot.is_deployed)

    def test_materialized_slot_does_not_report_vault_slot_fee_again(self):
        from common.tests.job_helpers import queued_args

        def open_callbacks():
            return [
                args["payload"]
                for args in queued_args("common.deliver_saas_callback")
                if args["payload"]["event"] == "vault_slot.created"
            ]

        # setUp 建 ETH 行时已报过一次充值用户开通费
        self.assertEqual(len(open_callbacks()), 1)

        load_owned_addresses_for_candidates(
            chain=self.bsc,
            addresses={self.deposit_address},
        )

        # 跨链补建的 BSC 行与 ETH 行同地址，不再上报
        self.assertTrue(
            VaultSlot.objects.filter(chain=self.bsc, customer=self.customer).exists()
        )
        self.assertEqual(len(open_callbacks()), 1)

    def test_materialize_is_idempotent_across_repeated_windows(self):
        load_owned_addresses_for_candidates(
            chain=self.bsc,
            addresses={self.deposit_address},
        )
        owned = load_owned_addresses_for_candidates(
            chain=self.bsc,
            addresses={self.deposit_address},
        )

        self.assertEqual(owned, frozenset({self.deposit_address}))
        self.assertEqual(
            VaultSlot.objects.filter(
                chain=self.bsc,
                customer=self.customer,
                usage=VaultSlotUsage.DEPOSIT,
            ).count(),
            1,
        )

    def test_mainnet_deposit_is_not_materialized_on_testnet(self):
        # 主网/测试网隔离：生产项目（is_test=False）的充值地址不得被补建到测试网，
        # 否则测试币会进入生产 Deposit/回调记账。
        sepolia = make_evm_chain(code=ChainCode.Sepolia, rpc="http://sepolia.local")

        owned = load_owned_addresses_for_candidates(
            chain=sepolia,
            addresses={self.deposit_address},
        )

        self.assertEqual(owned, frozenset())
        self.assertFalse(VaultSlot.objects.filter(chain=sepolia).exists())

    def test_existing_target_slot_with_different_address_not_owned(self):
        # 该客户在本链已有「地址不同」的历史槽位（基础合约地址曾轮换）：候选地址在本链并无
        # 对应 VaultSlot，不能标记 owned，否则 scanner 造出无法匹配 Deposit 的 Unmatched。
        rotated_address = Web3.to_checksum_address("0x" + "33" * 20)
        VaultSlot.objects.create(
            customer=self.customer,
            usage=VaultSlotUsage.DEPOSIT,
            chain=self.bsc,
            address=rotated_address,
            salt=b"\x07" * 32,
        )

        owned = load_owned_addresses_for_candidates(
            chain=self.bsc,
            addresses={self.deposit_address},
        )

        self.assertEqual(owned, frozenset())
        slot = VaultSlot.objects.get(chain=self.bsc, customer=self.customer)
        self.assertEqual(slot.address, rotated_address)

    def test_recompute_mismatch_is_rejected(self):
        # 源行地址与 (vault, salt) 不自洽：本链复算结果不等于该地址，拒绝补建，
        # 避免把无法在本链部署归集的地址记成充值。
        bogus_customer = Customer.objects.create(
            project=self.project,
            uid="xchain-bogus",
        )
        bogus_address = Web3.to_checksum_address("0x" + "22" * 20)
        VaultSlot.objects.create(
            customer=bogus_customer,
            usage=VaultSlotUsage.DEPOSIT,
            chain=self.eth,
            address=bogus_address,
            salt=b"\x09" * 32,
        )

        owned = load_owned_addresses_for_candidates(
            chain=self.bsc,
            addresses={bogus_address},
        )

        self.assertEqual(owned, frozenset())
        self.assertFalse(
            VaultSlot.objects.filter(
                chain=self.bsc,
                customer=bogus_customer,
            ).exists()
        )

    def test_invoice_slot_is_not_materialized_cross_chain(self):
        # 账单槽位跨链不自动补建：付错链与充值语义不同。
        invoice_salt = VaultSlot.build_salt(
            usage=VaultSlotUsage.INVOICE,
            project_id=self.project.pk,
            invoice_index=7,
        )
        invoice_address = predict_address(
            chain=self.eth,
            vault=self.vault_address,
            salt=invoice_salt,
        )
        VaultSlot.objects.create(
            usage=VaultSlotUsage.INVOICE,
            chain=self.eth,
            project=self.project,
            invoice_index=7,
            address=invoice_address,
            salt=invoice_salt,
        )

        owned = load_owned_addresses_for_candidates(
            chain=self.bsc,
            addresses={invoice_address},
        )

        self.assertEqual(owned, frozenset())
        self.assertFalse(
            VaultSlot.objects.filter(
                chain=self.bsc,
                usage=VaultSlotUsage.INVOICE,
            ).exists()
        )
