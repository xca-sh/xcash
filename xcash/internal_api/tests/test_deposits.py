from unittest.mock import Mock
from unittest.mock import patch

import pytest
from web3 import Web3

from chains.models import ChainType
from chains.models import Wallet
from projects.models import Project
from projects.models import RecipientAddress
from projects.models import RecipientAddressUsage

AUTH_HEADER = "Bearer test-internal-token"


@pytest.mark.django_db
class TestInternalDepositAddressByChainType:
    @patch("chains.signer.get_signer_backend")
    def test_chain_type_allocates_address_without_active_chain(
        self, get_signer_backend_mock, client, settings
    ):
        settings.INTERNAL_API_TOKEN = "test-internal-token"
        signer_backend = Mock()
        signer_backend.derive_address.return_value = Web3.to_checksum_address(
            "0x000000000000000000000000000000000000d111"
        )
        get_signer_backend_mock.return_value = signer_backend

        project = Project.objects.create(
            name="internal-deposit-project",
            wallet=Wallet.objects.create(),
        )
        RecipientAddress.objects.create(
            project=project,
            chain_type=ChainType.EVM,
            address=Web3.to_checksum_address(
                "0x000000000000000000000000000000000000d199"
            ),
            usage=RecipientAddressUsage.DEPOSIT_COLLECTION,
        )

        response = client.get(
            f"/internal/v1/projects/{project.appid}/deposits/address",
            {"uid": "user-1", "chain_type": "evm"},
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )

        assert response.status_code == 200
        assert response.json() == {
            "deposit_address": "0x000000000000000000000000000000000000D111"
        }
        signer_backend.derive_address.assert_called_once()

    def test_unknown_chain_code_still_returns_invalid_chain(self, client, settings):
        settings.INTERNAL_API_TOKEN = "test-internal-token"
        project = Project.objects.create(
            name="internal-deposit-project-2",
            wallet=Wallet.objects.create(),
        )

        response = client.get(
            f"/internal/v1/projects/{project.appid}/deposits/address",
            {"uid": "user-1", "chain": "missing-chain"},
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )

        assert response.status_code == 400
        assert response.json() == {
            "code": "2000",
            "message": "无效链",
            "detail": "",
        }
