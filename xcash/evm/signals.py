from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_delete
from django.db.models.signals import post_save
from django.dispatch import receiver

from chains.models import ChainType
from currencies.models import ChainToken
from currencies.models import Crypto
from evm.models import DepositSlot
from evm.scanner.watchers import clear_evm_chain_tokens_cache
from evm.scanner.watchers import clear_evm_watched_addresses_cache
from evm.scanner.watchers import load_watch_set
from evm.scanner.watchers import refresh_evm_watched_addresses
from invoices.models import InvoiceBillingMode
from invoices.models import InvoicePaySlot


def _refresh_evm_watched_addresses_on_commit() -> None:
    clear_evm_watched_addresses_cache()
    transaction.on_commit(refresh_evm_watched_addresses)


def _refresh_evm_chain_tokens_on_commit(*, chain_token: ChainToken) -> None:
    chain = chain_token.chain
    if chain.type != ChainType.EVM:
        return
    clear_evm_chain_tokens_cache(chain=chain)
    transaction.on_commit(lambda: load_watch_set(chain=chain, refresh=True))


def _refresh_crypto_chain_tokens_on_commit(*, crypto: Crypto) -> None:
    chains = [
        chain_token.chain
        for chain_token in ChainToken.objects.select_related("chain").filter(
            crypto=crypto,
            chain__type=ChainType.EVM,
        )
    ]
    for chain in chains:
        clear_evm_chain_tokens_cache(chain=chain)

    def refresh_chain_watch_sets() -> None:
        for chain in chains:
            load_watch_set(chain=chain, refresh=True)

    transaction.on_commit(refresh_chain_watch_sets)


@receiver(post_save, sender=DepositSlot)
@receiver(post_delete, sender=DepositSlot)
def refresh_watch_set_when_deposit_slot_changes(
    sender, instance: DepositSlot, **kwargs
):
    _refresh_evm_watched_addresses_on_commit()


@receiver(post_save, sender=InvoicePaySlot)
@receiver(post_delete, sender=InvoicePaySlot)
def refresh_watch_set_when_invoice_pay_slot_changes(
    sender,
    instance: InvoicePaySlot,
    **kwargs,
):
    if instance.billing_mode != InvoiceBillingMode.CONTRACT:
        return
    _refresh_evm_watched_addresses_on_commit()


@receiver(post_save, sender=ChainToken)
@receiver(post_delete, sender=ChainToken)
def refresh_watch_set_when_chain_token_changes(
    sender,
    instance: ChainToken,
    **kwargs,
):
    _refresh_evm_chain_tokens_on_commit(chain_token=instance)


@receiver(post_save, sender=Crypto)
def refresh_watch_set_when_crypto_changes(sender, instance: Crypto, **kwargs):
    _refresh_crypto_chain_tokens_on_commit(crypto=instance)
