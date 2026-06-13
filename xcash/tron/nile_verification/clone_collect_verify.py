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
    """按生产「部署→归集」两段式路径做 Nile 验收。

    第一笔 deployVaultSlot 把 slot 部署到预测地址,第二笔对 slot 直调
    collect(token) 清扫 TRC20。原生 TRX 的 collect(address(0)) 由 A/B 激活
    脚本覆盖。生产里这两步由独立 TxTask 承载;此脚本按相同顺序各发一笔交易,
    验证预测地址、部署与重复归集路径在实链上的行为。
    """
    setup_django()
    from tron.adapter import TronAdapter
    from tron.client import TronHttpClient
    from tron.contracts_codec import predict_tron_vault_slot_address
    from tron.intents import build_vault_slot_collect_intent
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
    salt_hex = env_required("TRON_VAULT_SLOT_SALT_HEX")
    salt = bytes.fromhex(salt_hex.removeprefix("0x"))
    expected_slot = predict_tron_vault_slot_address(
        vault=vault,
        salt=salt,
        factory=factory,
        vault_slot_implementation=implementation,
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

    # 与生产前置闸门同构:已部署跳过 deploy 直接归集。脚本重复执行时第二次
    # 自然落在「已部署槽位纯 collect」路径上,这正是需要实链验收的重复归集面。
    slot_deployed = TronAdapter().is_contract(chain, expected_slot)
    emit(f"slot_deployed={slot_deployed}")
    if not slot_deployed:
        deploy_intent = build_vault_slot_deploy_intent(
            sender=type("Sender", (), {"address": owner})(),
            chain=chain,
            factory_address=factory,
            vault_address=vault,
            salt=salt,
        )
        unsigned = client.trigger_smart_contract(
            owner_address=owner,
            contract_address=deploy_intent.to,
            function_selector=deploy_intent.function_selector,
            parameter=deploy_intent.parameter,
            fee_limit=deploy_intent.fee_limit,
        )
        deploy_tx_id = sign_and_broadcast(
            client=client,
            private_key=private_key,
            transaction=unsigned["transaction"],
            broadcast=args.broadcast,
        )
        if not args.broadcast:
            emit("dry_run=未广播部署,slot 尚不存在,collect 步骤待部署确认后重跑")
            return
        if args.wait:
            emit(f"deploy_receipt={wait_tx_info(client=client, tx_id=deploy_tx_id)}")

    collect_intent = build_vault_slot_collect_intent(
        sender=type("Sender", (), {"address": owner})(),
        chain=chain,
        slot_address=expected_slot,
        token_address=token,
    )
    triggered = client.trigger_smart_contract(
        owner_address=owner,
        contract_address=collect_intent.to,
        function_selector=collect_intent.function_selector,
        parameter=collect_intent.parameter,
        fee_limit=collect_intent.fee_limit,
    )
    # 干跑(不广播)时 slot 尚未部署,triggersmartcontract 返回不含 transaction 的
    # 错误体;广播流程下 deploy 已先确认、slot 必然存在。缺 transaction 即如实报出。
    if "transaction" not in triggered:
        raise SystemExit(f"collect 构造失败,节点响应:{triggered}")
    collect_tx_id = sign_and_broadcast(
        client=client,
        private_key=private_key,
        transaction=triggered["transaction"],
        broadcast=args.broadcast,
    )
    if args.wait and args.broadcast:
        emit(f"collect_receipt={wait_tx_info(client=client, tx_id=collect_tx_id)}")


if __name__ == "__main__":
    main()
