from __future__ import annotations

import argparse
import secrets

from verification_common import emit
from verification_common import env_int
from verification_common import env_optional
from verification_common import env_required
from verification_common import nile_chain
from verification_common import nile_vault_address
from verification_common import setup_django
from verification_common import sign_and_broadcast
from verification_common import wait_tx_info


def main() -> None:
    setup_django()
    from tron.client import TronHttpClient
    from tron.contracts_codec import predict_tron_vault_slot_address
    from tron.intents import build_vault_slot_deploy_intent

    parser = argparse.ArgumentParser()
    parser.add_argument("--broadcast", action="store_true")
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()

    chain = nile_chain()
    owner = env_required("TRON_NILE_OWNER_ADDRESS")
    private_key = env_required("TRON_NILE_PRIVATE_KEY")
    factory = env_required("TRON_VAULT_SLOT_FACTORY_ADDRESS")
    implementation = env_required("TRON_VAULT_SLOT_IMPLEMENTATION_ADDRESS")
    vault = nile_vault_address(owner_address=owner)
    salt_hex = env_optional("TRON_VAULT_SLOT_SALT_HEX")
    salt = bytes.fromhex(salt_hex.removeprefix("0x")) if salt_hex else secrets.token_bytes(32)
    fee_limit = env_int("TRON_VAULT_SLOT_FEE_LIMIT", 300_000_000)

    predicted = predict_tron_vault_slot_address(
        vault=vault,
        salt=salt,
        factory=factory,
        vault_slot_implementation=implementation,
    )
    emit(f"salt_hex={salt.hex()}")
    emit(f"predicted={predicted}")

    client = TronHttpClient(chain=chain)
    intent = build_vault_slot_deploy_intent(
        sender=type("Sender", (), {"address": owner})(),
        chain=chain,
        factory_address=factory,
        vault_address=vault,
        salt=salt,
    )
    unsigned = client.trigger_smart_contract(
        owner_address=owner,
        contract_address=intent.to,
        function_selector=intent.function_selector,
        parameter=intent.parameter,
        fee_limit=fee_limit,
    )
    tx_id = sign_and_broadcast(
        client=client,
        private_key=private_key,
        transaction=unsigned["transaction"],
        broadcast=args.broadcast,
    )
    if args.wait and args.broadcast:
        emit(f"receipt={wait_tx_info(client=client, tx_id=tx_id)}")
    emit(f"compare_expected_slot={predicted}")


if __name__ == "__main__":
    main()
