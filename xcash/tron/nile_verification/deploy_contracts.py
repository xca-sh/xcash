from __future__ import annotations

import argparse
import json
from pathlib import Path

import eth_abi
from verification_common import emit
from verification_common import env_int
from verification_common import env_required
from verification_common import nile_chain
from verification_common import setup_django
from verification_common import sign_and_broadcast
from verification_common import wait_tx_info

DEFAULT_DEPLOY_FEE_LIMIT = 1_500_000_000


def main() -> None:
    setup_django()
    from tron.client import TronHttpClient
    from tron.codec import TronAddressCodec
    from tron.contracts_codec import tron_base58_to_evm_address

    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", action="store_true", default=True)
    args = parser.parse_args()

    chain = nile_chain()
    owner = env_required("TRON_NILE_OWNER_ADDRESS")
    private_key = env_required("TRON_NILE_PRIVATE_KEY")
    fee_limit = env_int("TRON_VAULT_SLOT_DEPLOY_FEE_LIMIT", DEFAULT_DEPLOY_FEE_LIMIT)

    client = TronHttpClient(chain=chain)

    implementation_artifact = load_artifact(
        "XcashVaultSlot.sol",
        "XcashVaultSlot",
    )
    implementation_unsigned = client.deploy_contract(
        owner_address=owner,
        name="XcashVaultSlot",
        abi=implementation_artifact["abi"],
        bytecode=artifact_bytecode(implementation_artifact),
        fee_limit=fee_limit,
    )
    implementation_tx_id = sign_and_broadcast(
        client=client,
        private_key=private_key,
        transaction=extract_transaction(implementation_unsigned),
        broadcast=True,
    )
    implementation_receipt = (
        wait_tx_info(client=client, tx_id=implementation_tx_id) if args.wait else {}
    )
    implementation_address = extract_contract_address(
        tron_address_codec=TronAddressCodec,
        payloads=(implementation_unsigned, implementation_receipt),
    )
    emit(f"TRON_VAULT_SLOT_IMPLEMENTATION_ADDRESS={implementation_address}")

    factory_artifact = load_artifact(
        "XcashVaultSlotFactory.sol",
        "XcashVaultSlotFactory",
    )
    factory_parameter = eth_abi.encode(
        ["address"],
        [tron_base58_to_evm_address(implementation_address)],
    ).hex()
    factory_unsigned = client.deploy_contract(
        owner_address=owner,
        name="XcashVaultSlotFactory",
        abi=factory_artifact["abi"],
        bytecode=artifact_bytecode(factory_artifact),
        parameter=factory_parameter,
        fee_limit=fee_limit,
    )
    factory_tx_id = sign_and_broadcast(
        client=client,
        private_key=private_key,
        transaction=extract_transaction(factory_unsigned),
        broadcast=True,
    )
    factory_receipt = wait_tx_info(client=client, tx_id=factory_tx_id) if args.wait else {}
    factory_address = extract_contract_address(
        tron_address_codec=TronAddressCodec,
        payloads=(factory_unsigned, factory_receipt),
    )
    emit(f"TRON_VAULT_SLOT_FACTORY_ADDRESS={factory_address}")
    emit("")
    emit("回填到 xcash/tron/nile_verification/.env：")
    emit(f"TRON_VAULT_SLOT_IMPLEMENTATION_ADDRESS={implementation_address}")
    emit(f"TRON_VAULT_SLOT_FACTORY_ADDRESS={factory_address}")


def contracts_root() -> Path:
    return Path(__file__).resolve().parents[1] / "contracts"


def load_artifact(source_file: str, contract_name: str) -> dict:
    artifact_path = contracts_root() / "out" / source_file / f"{contract_name}.json"
    if not artifact_path.exists():
        raise SystemExit(
            f"{artifact_path} does not exist; run `forge build` in xcash/tron/contracts first"
        )
    return json.loads(artifact_path.read_text(encoding="utf-8"))


def artifact_bytecode(artifact: dict) -> str:
    bytecode = artifact.get("bytecode")
    if isinstance(bytecode, dict):
        bytecode = bytecode.get("object")
    bytecode = str(bytecode or "").removeprefix("0x")
    if not bytecode:
        raise SystemExit("artifact bytecode is empty")
    return bytecode


def extract_transaction(payload: dict) -> dict:
    transaction = payload.get("transaction")
    if isinstance(transaction, dict):
        return transaction
    if "raw_data_hex" in payload:
        return payload
    raise SystemExit(f"deploy response has no transaction: {payload}")


def extract_contract_address(*, tron_address_codec, payloads: tuple[dict, ...]) -> str:
    for payload in payloads:
        for key in ("contract_address", "contractAddress"):
            value = payload.get(key)
            if not value:
                continue
            address = str(value)
            if tron_address_codec.is_valid_base58(address):
                return tron_address_codec.normalize_base58(address)
            return tron_address_codec.hex41_to_base58(address)
    raise SystemExit(
        "deploy response did not include contract address; check the printed tx_id on Nile explorer"
    )


if __name__ == "__main__":
    main()
