from __future__ import annotations

import argparse

from verification_common import emit
from verification_common import env_required
from verification_common import nile_vault_address
from verification_common import setup_django


def main() -> None:
    setup_django()
    from eth_utils import keccak
    from tron.contracts_codec import predict_tron_vault_slot_address

    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--salt-prefix", default="xcash:tron-nile-verification")
    args = parser.parse_args()

    factory = env_required("TRON_VAULT_SLOT_FACTORY_ADDRESS")
    implementation = env_required("TRON_VAULT_SLOT_IMPLEMENTATION_ADDRESS")
    owner = env_required("TRON_NILE_OWNER_ADDRESS")
    vault = nile_vault_address(owner_address=owner)

    emit("index,salt_hex,predicted_address")
    for index in range(args.count):
        salt = keccak(f"{args.salt_prefix}:{index}".encode())
        predicted = predict_tron_vault_slot_address(
            vault=vault,
            salt=salt,
            factory=factory,
            vault_slot_implementation=implementation,
        )
        emit(f"{index},{salt.hex()},{predicted}")


if __name__ == "__main__":
    main()
