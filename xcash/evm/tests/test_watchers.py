from chains.models import Address, AddressUsage, Chain, ChainType, Wallet
from currencies.models import ChainToken, Crypto
from django.core.cache import cache
from django.test import TestCase, override_settings
from evm.scanner.watchers import (
    clear_evm_watch_set_cache,
    load_evm_system_addresses,
    load_watch_set,
)
from projects.models import Project, RecipientAddress, RecipientAddressUsage
from web3 import Web3

WATCHER_TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "evm-watch-set-tests",
    }
}


@override_settings(CACHES=WATCHER_TEST_CACHES)
class EvmWatchSetCacheTests(TestCase):
    def setUp(self):
        cache.clear()
        self.native = Crypto.objects.create(
            name="Watcher Native",
            symbol="WNATIVE",
            coingecko_id="watcher-native",
        )
        self.chain = Chain.objects.create(
            code="watcher-chain",
            name="Watcher Chain",
            type=ChainType.EVM,
            chain_id=88_001,
            rpc="http://watcher.local",
            native_coin=self.native,
            confirm_block_count=6,
            active=True,
        )
        self.token = Crypto.objects.create(
            name="Watcher Token",
            symbol="WTKN",
            coingecko_id="watcher-token",
            decimals=18,
        )
        self.token_deployment = ChainToken.objects.create(
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
            usage=AddressUsage.Deposit,
            bip44_account=0,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000bb"
            ),
        )

    def tearDown(self):
        clear_evm_watch_set_cache()
        cache.clear()

    def test_load_watch_set_reuses_cache_until_refresh_requested(self):
        initial_watch_set = load_watch_set(chain=self.chain, refresh=True)
        self.assertIn(self.address.address, initial_watch_set.watched_addresses)

        Address.objects.filter(pk=self.address.pk).update(chain_type=ChainType.BITCOIN)

        cached_watch_set = load_watch_set(chain=self.chain)
        self.assertIn(self.address.address, cached_watch_set.watched_addresses)

        refreshed_watch_set = load_watch_set(chain=self.chain, refresh=True)
        self.assertNotIn(self.address.address, refreshed_watch_set.watched_addresses)

    def test_load_evm_system_addresses_excludes_recipient_addresses(self):
        recipient_address = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000cd"
        )
        RecipientAddress.objects.create(
            name="watcher-recipient-system-cache",
            project=self._create_project(),
            chain_type=ChainType.EVM,
            address=recipient_address,
            usage=RecipientAddressUsage.INVOICE,
        )

        system_addresses = load_evm_system_addresses(refresh=True)

        self.assertIn(self.address.address, system_addresses)
        self.assertNotIn(recipient_address, system_addresses)

    def test_address_save_refreshes_cached_watch_set_after_commit(self):
        load_watch_set(chain=self.chain, refresh=True)
        new_address = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000cc"
        )

        with self.captureOnCommitCallbacks(execute=True):
            Address.objects.create(
                wallet=self.wallet,
                chain_type=ChainType.EVM,
                usage=AddressUsage.Deposit,
                bip44_account=0,
                address_index=1,
                address=new_address,
            )

        watch_set = load_watch_set(chain=self.chain)
        self.assertIn(new_address, watch_set.watched_addresses)

    def test_recipient_address_save_refreshes_cached_watch_set_after_commit(self):
        load_watch_set(chain=self.chain, refresh=True)
        recipient_address = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000dd"
        )

        with self.captureOnCommitCallbacks(execute=True):
            RecipientAddress.objects.create(
                name="watcher-recipient",
                project=self._create_project(),
                chain_type=ChainType.EVM,
                address=recipient_address,
                usage=RecipientAddressUsage.INVOICE,
            )

        watch_set = load_watch_set(chain=self.chain)
        self.assertIn(recipient_address, watch_set.watched_addresses)

    def test_chain_token_save_refreshes_cached_token_set_after_commit(self):
        load_watch_set(chain=self.chain, refresh=True)
        new_token = Crypto.objects.create(
            name="Watcher Token Two",
            symbol="WTKN2",
            coingecko_id="watcher-token-two",
            decimals=6,
        )
        token_address = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000ee"
        )

        with self.captureOnCommitCallbacks(execute=True):
            ChainToken.objects.create(
                crypto=new_token,
                chain=self.chain,
                address=token_address,
                decimals=6,
            )

        watch_set = load_watch_set(chain=self.chain)
        self.assertIn(token_address, watch_set.tokens_by_address)

    def test_chain_token_delete_refreshes_cached_token_set_after_commit(self):
        initial_watch_set = load_watch_set(chain=self.chain, refresh=True)
        self.assertIn(
            self.token_deployment.address, initial_watch_set.tokens_by_address
        )

        with self.captureOnCommitCallbacks(execute=True):
            self.token_deployment.delete()

        watch_set = load_watch_set(chain=self.chain)
        self.assertNotIn(self.token_deployment.address, watch_set.tokens_by_address)

    def test_crypto_active_change_refreshes_cached_token_set_after_commit(self):
        initial_watch_set = load_watch_set(chain=self.chain, refresh=True)
        self.assertIn(
            self.token_deployment.address, initial_watch_set.tokens_by_address
        )

        with self.captureOnCommitCallbacks(execute=True):
            self.token.active = False
            self.token.save(update_fields=["active"])

        watch_set = load_watch_set(chain=self.chain)
        self.assertNotIn(self.token_deployment.address, watch_set.tokens_by_address)

    def _create_project(self) -> Project:
        return Project.objects.create(
            name="watcher-project",
            wallet=Wallet.objects.create(),
            webhook="https://example.com/webhook",
        )
