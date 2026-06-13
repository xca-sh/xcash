from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from chains.constants import TRON_VAULT_SLOT_CONTRACT_ADDRESSES
from chains.constants import ChainCode
from chains.constants import ChainType
from chains.constants import VaultSlotContractAddresses
from chains.models import Chain


@pytest.mark.django_db
def test_chain_unique_per_name():
    # Chain.save() 内置 full_clean()，validate_unique 会在 DB 层 IntegrityError
    # 之前先抛 ValidationError；唯一性约束仍由 DB 层兜底，但用户面错误是 ValidationError。
    Chain.objects.create(code=ChainCode.BSC, active=False)
    with pytest.raises(ValidationError):
        Chain.objects.create(code=ChainCode.BSC, active=False)


@pytest.mark.django_db
def test_chain_native_coin_get_or_create():
    # 行为：native_coin 惰性 get_or_create 并在同一 Chain 上幂等返回同一条 Crypto。
    chain = Chain.objects.create(code=ChainCode.Ethereum, active=False)
    coin = chain.native_coin
    same = chain.native_coin
    assert same.pk == coin.pk


@pytest.mark.django_db
def test_chain_invalid_choice_rejected():
    chain = Chain(code="not-a-real-chain")
    with pytest.raises(ValidationError):
        chain.full_clean()


@pytest.mark.django_db
def test_clean_skips_when_rpc_empty():
    chain = Chain(code=ChainCode.Ethereum, rpc="")
    chain.full_clean()


@pytest.mark.django_db
def test_clean_skips_rpc_check_when_evm_chain_inactive():
    chain = Chain(code=ChainCode.Ethereum, rpc="http://fake.rpc", active=False)
    with patch("chains.models.Web3") as mock_w3:
        chain.full_clean()

    mock_w3.assert_not_called()


@pytest.mark.django_db
def test_clean_skips_for_tron():
    chain = Chain(code=ChainCode.Tron, rpc="", tron_api_key="key")
    chain.full_clean()


def test_vault_slot_contract_addresses_for_evm_chain():
    from evm.constants import XCASH_VAULT_SLOT_FACTORY_ADDRESS
    from evm.constants import XCASH_VAULT_SLOT_IMPLEMENTATION_ADDRESS

    chain = Chain(code=ChainCode.Ethereum, type=ChainType.EVM)

    addresses = chain.vault_slot_contract_addresses()

    assert addresses.factory == XCASH_VAULT_SLOT_FACTORY_ADDRESS
    assert addresses.implementation == XCASH_VAULT_SLOT_IMPLEMENTATION_ADDRESS


def test_vault_slot_contract_addresses_for_tron_chain_uses_chain_code():
    mainnet_addresses = VaultSlotContractAddresses(
        factory="TJRabPrwbZy45sbavfcjinPJC18kjpRTv8",
        implementation="TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb",
    )
    nile_addresses = VaultSlotContractAddresses(
        factory="TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj",
        implementation="TJRabPrwbZy45sbavfcjinPJC18kjpRTv8",
    )

    with patch.dict(
        TRON_VAULT_SLOT_CONTRACT_ADDRESSES,
        {
            ChainCode.Tron: mainnet_addresses,
            ChainCode.Nile: nile_addresses,
        },
    ):
        mainnet = Chain(code=ChainCode.Tron, type=ChainType.TRON)
        nile = Chain(code=ChainCode.Nile, type=ChainType.TRON)

        assert mainnet.vault_slot_contract_addresses() == mainnet_addresses
        assert nile.vault_slot_contract_addresses() == nile_addresses


@pytest.mark.django_db
def test_active_evm_chain_requires_rpc():
    with pytest.raises(ValidationError):
        Chain.objects.create(code=ChainCode.Ethereum, rpc="", active=True)


@pytest.mark.django_db
def test_active_tron_chain_requires_api_key():
    with pytest.raises(ValidationError):
        Chain.objects.create(code=ChainCode.Tron, tron_api_key="", active=True)


@pytest.mark.django_db
def test_active_tron_chain_rejects_clearing_api_key():
    chain = Chain.objects.create(
        code=ChainCode.Tron,
        tron_api_key="configured",
        active=True,
    )

    chain.tron_api_key = ""
    with pytest.raises(ValidationError):
        chain.save(update_fields={"tron_api_key"})

    chain.refresh_from_db()
    assert chain.active is True
    assert chain.tron_api_key == "configured"


@pytest.mark.django_db
def test_active_chain_runtime_config_database_constraint():
    with pytest.raises(IntegrityError):
        Chain.objects.bulk_create(
            [
                Chain(
                    code=ChainCode.Ethereum,
                    type=ChainType.EVM,
                    rpc="",
                    active=True,
                )
            ]
        )


@pytest.mark.django_db
def test_clean_accepts_matching_rpc():
    chain = Chain(
        code=ChainCode.Ethereum,
        type=ChainType.EVM,
        rpc="http://fake.rpc",
        active=True,
    )
    with patch("chains.models.Web3") as mock_w3:
        mock_w3.HTTPProvider.return_value = object()
        mock_w3.return_value.eth.chain_id = 1
        chain.full_clean()


@pytest.mark.django_db
def test_clean_rejects_mismatching_rpc():
    chain = Chain(
        code=ChainCode.Ethereum,
        type=ChainType.EVM,
        rpc="http://fake.rpc",
        active=True,
    )
    with patch("chains.models.Web3") as mock_w3:
        mock_w3.HTTPProvider.return_value = object()
        mock_w3.return_value.eth.chain_id = 56  # BSC, not Ethereum
        with pytest.raises(ValidationError) as exc_info:
            chain.full_clean()
        assert "rpc" in exc_info.value.message_dict


@pytest.mark.django_db
def test_clean_rejects_unreachable_rpc():
    chain = Chain(
        code=ChainCode.Ethereum,
        type=ChainType.EVM,
        rpc="http://fake.rpc",
        active=True,
    )
    with patch("chains.models.Web3") as mock_w3:
        mock_w3.HTTPProvider.return_value = object()
        type(mock_w3.return_value.eth).chain_id = property(
            lambda self: (_ for _ in ()).throw(ConnectionError("boom"))
        )
        with pytest.raises(ValidationError) as exc_info:
            chain.full_clean()
        assert "rpc" in exc_info.value.message_dict
