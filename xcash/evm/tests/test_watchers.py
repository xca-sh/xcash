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
