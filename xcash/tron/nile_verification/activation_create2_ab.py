from __future__ import annotations

import argparse
import time

from verification_common import emit
from verification_common import env_int
from verification_common import env_required
from verification_common import nile_chain
from verification_common import nile_vault_address
from verification_common import setup_django
from verification_common import sign_and_broadcast
from verification_common import wait_tx_info

# collect(address(0)) 的原生币零地址。build_vault_slot_collect_intent 内部会把它
# ABI 编码成 address(0)，命中 XcashVaultSlot.collect 的原生币分支。
NATIVE_TOKEN_ADDRESS = "0x0000000000000000000000000000000000000000"


def native_balance_sun(*, client, address: str) -> int:
    """读取地址的原生 TRX 余额（sun）。账户未激活时 getaccount 返回 {}，按 0 处理。"""
    account = client.get_account(address=address)
    return int(account.get("balance", 0) or 0)


def main() -> None:
    setup_django()
    from eth_utils import keccak
    from tron.client import TronHttpClient
    from tron.contracts_codec import predict_tron_vault_slot_address
    from tron.intents import build_contract_call_intent
    from tron.intents import build_vault_slot_collect_intent
    from tron.intents import build_vault_slot_deploy_intent
    from tron.intents import trc20_balance_of_parameter

    from chains.models import TxTaskType

    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=("a", "b"), required=True)
    parser.add_argument("--broadcast", action="store_true")
    parser.add_argument("--wait", action="store_true")
    # 每次跑用全新盐 → 全新反事实地址，避免撞上历史已部署的 slot 导致 deployVaultSlot
    # 因 CREATE2 目标地址已存在而 revert。传 --salt-tag 可复现指定地址。
    parser.add_argument("--salt-tag", default="")
    args = parser.parse_args()

    chain = nile_chain()
    owner = env_required("TRON_NILE_OWNER_ADDRESS")
    private_key = env_required("TRON_NILE_PRIVATE_KEY")
    factory = env_required("TRON_VAULT_SLOT_FACTORY_ADDRESS")
    implementation = env_required("TRON_VAULT_SLOT_IMPLEMENTATION_ADDRESS")
    vault = nile_vault_address(owner_address=owner)
    token = env_required("TRON_USDT_CONTRACT_ADDRESS")
    fee_limit = env_int("TRON_VAULT_SLOT_FEE_LIMIT", 300_000_000)
    salt_tag = args.salt_tag or str(time.time_ns())
    salt = keccak(f"xcash:tron-activation:{args.case}:{salt_tag}".encode())
    emit(f"salt_tag={salt_tag}")
    predicted = predict_tron_vault_slot_address(
        vault=vault,
        salt=salt,
        factory=factory,
        vault_slot_implementation=implementation,
    )
    emit(f"case={args.case}")
    emit(f"salt_hex={salt.hex()}")
    emit(f"predicted={predicted}")
    client = TronHttpClient(chain=chain)

    if args.case == "a":
        unsigned = client.create_trx_transfer(
            owner_address=owner,
            to_address=predicted,
            amount_sun=1_000_000,
        )
        tx_id = sign_and_broadcast(
            client=client,
            private_key=private_key,
            transaction=unsigned,
            broadcast=args.broadcast,
        )
        if args.wait and args.broadcast:
            emit(f"activation_receipt={wait_tx_info(client=client, tx_id=tx_id)}")
    else:
        parameter = trc20_balance_of_parameter(predicted)
        intent = build_contract_call_intent(
            sender=type("Sender", (), {"address": owner})(),
            chain=chain,
            contract_address=token,
            function_selector_value="transfer(address,uint256)",
            parameter=parameter + f"{1:064x}",
            fee_limit=fee_limit,
            tx_type=TxTaskType.VaultSlotCollect,
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
            emit(f"trc20_prefund_receipt={wait_tx_info(client=client, tx_id=tx_id)}")

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
        fee_limit=fee_limit,
    )
    deploy_tx_id = sign_and_broadcast(
        client=client,
        private_key=private_key,
        transaction=unsigned["transaction"],
        broadcast=args.broadcast,
    )
    if args.wait and args.broadcast:
        emit(f"deploy_receipt={wait_tx_info(client=client, tx_id=deploy_tx_id)}")

    # 仅 Case A 验证“原生 TRX 真能被扫回 vault”这一寸：部署后 slot 上仍躺着激活用的
    # 1 TRX，调 collect(address(0)) 经 vault().call{value:} 转出。这是此前 A/B 与
    # clone_collect 都没覆盖的一步——Tron 上 TransferContract 不触发 receive()，原生 TRX
    # 永远静置、只能靠显式 collect 出账，是原生币唯一出口，必须实测。TRC20 的 collect
    # 已由 clone_collect_verify.py 覆盖，这里不重复。
    if args.case == "a":
        slot_before = native_balance_sun(client=client, address=predicted)
        vault_before = native_balance_sun(client=client, address=vault)
        emit(f"slot_native_before_sun={slot_before}")
        emit(f"vault_native_before_sun={vault_before}")

        collect_intent = build_vault_slot_collect_intent(
            sender=type("Sender", (), {"address": owner})(),
            chain=chain,
            slot_address=predicted,
            token_address=NATIVE_TOKEN_ADDRESS,
        )
        triggered = client.trigger_smart_contract(
            owner_address=owner,
            contract_address=collect_intent.to,
            function_selector=collect_intent.function_selector,
            parameter=collect_intent.parameter,
            fee_limit=fee_limit,
        )
        # 干跑（不广播）时 slot 尚未部署，triggersmartcontract 返回不含 transaction 的错误体；
        # 广播流程下 deploy 已先确认、slot 必然存在。缺 transaction 即如实报出节点响应便于排查。
        if "transaction" not in triggered:
            raise SystemExit(f"native collect 构造失败，节点响应：{triggered}")
        collect_tx_id = sign_and_broadcast(
            client=client,
            private_key=private_key,
            transaction=triggered["transaction"],
            broadcast=args.broadcast,
        )
        if args.wait and args.broadcast:
            emit(
                f"native_collect_receipt={wait_tx_info(client=client, tx_id=collect_tx_id)}"
            )
            slot_after = native_balance_sun(client=client, address=predicted)
            vault_after = native_balance_sun(client=client, address=vault)
            emit(f"slot_native_after_sun={slot_after}")
            emit(f"vault_native_after_sun={vault_after}")
            # 硬断言：collect(address(0)) 把 slot 全额 forward 给 vault，手续费由 owner
            # （tx 发起方）支付、不动 slot 余额，故 slot 必须清零。这是原生归集成功的决定性证据。
            if slot_after != 0:
                raise SystemExit(
                    f"native collect FAILED：slot 仍剩 {slot_after} sun，未能转出"
                )
            # vault 增量仅信息展示：测试里 vault 默认 == owner，而 owner 同时付手续费，
            # 净增量会被 gas 抵掉，故不对 vault 侧做硬断言。
            emit(f"vault_native_delta_sun={vault_after - vault_before}")
            emit("native_collect=OK：slot 已清零，原生 TRX 成功扫回 vault")

    emit("结论块：把 deploy_receipt 与（Case A）native_collect 结果粘回 docs/tron-vaultslot-migration.md Phase 0B。")


if __name__ == "__main__":
    main()
