from __future__ import annotations

import argparse

from verification_common import emit
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
    from tron.intents import build_vault_slot_ensure_collect_intent

    parser = argparse.ArgumentParser()
    parser.add_argument("--broadcast", action="store_true")
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()

    chain = nile_chain()
    owner = env_required("TRON_NILE_OWNER_ADDRESS")
    private_key = env_required("TRON_NILE_PRIVATE_KEY")
    factory = env_required("TRON_VAULT_SLOT_FACTORY_ADDRESS")
    template = env_required("TRON_VAULT_SLOT_TEMPLATE_ADDRESS")
    vault = nile_vault_address(owner_address=owner)
    salt_hex = env_required("TRON_VAULT_SLOT_SALT_HEX")
    salt = bytes.fromhex(salt_hex.removeprefix("0x"))
    expected_slot = predict_tron_vault_slot_address(
        vault=vault,
        salt=salt,
        factory=factory,
        vault_slot_template=template,
    )
    configured_slot = env_optional("TRON_VAULT_SLOT_ADDRESS")
    if configured_slot and configured_slot != expected_slot:
        raise SystemExit(
            "TRON_VAULT_SLOT_ADDRESS does not match salt/vault prediction: "
            f"{configured_slot} != {expected_slot}"
        )
    token = env_required("TRON_USDT_CONTRACT_ADDRESS")
    emit(f"expected_slot={expected_slot}")

    client = TronHttpClient(chain=chain)
    intent = build_vault_slot_ensure_collect_intent(
        sender=type("Sender", (), {"address": owner})(),
        chain=chain,
        factory_address=factory,
        vault_address=vault,
        salt=salt,
        token_address=token,
    )
    unsigned = client.trigger_smart_contract(
        owner_address=owner,
        contract_address=intent.to,
        function_selector=intent.function_selector,
        parameter=intent.parameter,
        fee_limit=intent.fee_limit,
    )
    tx_id = sign_and_broadcast(
        client=client,
        private_key=private_key,
        transaction=unsigned["transaction"],
        broadcast=args.broadcast,
    )
    if args.wait and args.broadcast:
        emit(f"receipt={wait_tx_info(client=client, tx_id=tx_id)}")


if __name__ == "__main__":
    main()
