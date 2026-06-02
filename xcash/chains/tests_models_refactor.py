from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from chains.constants import ChainCode
from chains.constants import ChainType
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
def test_clean_skips_for_tron():
    chain = Chain(code=ChainCode.Tron, rpc="", tron_api_key="key")
    chain.full_clean()


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
    chain = Chain(code=ChainCode.Ethereum, rpc="http://fake.rpc")
    with patch("chains.models.Web3") as mock_w3:
        mock_w3.HTTPProvider.return_value = object()
        mock_w3.return_value.eth.chain_id = 1
        chain.full_clean()


@pytest.mark.django_db
def test_clean_rejects_mismatching_rpc():
    chain = Chain(code=ChainCode.Ethereum, rpc="http://fake.rpc")
    with patch("chains.models.Web3") as mock_w3:
        mock_w3.HTTPProvider.return_value = object()
        mock_w3.return_value.eth.chain_id = 56  # BSC, not Ethereum
        with pytest.raises(ValidationError) as exc_info:
            chain.full_clean()
        assert "rpc" in exc_info.value.message_dict


@pytest.mark.django_db
def test_clean_rejects_unreachable_rpc():
    chain = Chain(code=ChainCode.Ethereum, rpc="http://fake.rpc")
    with patch("chains.models.Web3") as mock_w3:
        mock_w3.HTTPProvider.return_value = object()
        type(mock_w3.return_value.eth).chain_id = property(
            lambda self: (_ for _ in ()).throw(ConnectionError("boom"))
        )
        with pytest.raises(ValidationError) as exc_info:
            chain.full_clean()
        assert "rpc" in exc_info.value.message_dict
