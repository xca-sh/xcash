"""本地 VaultSlot 工厂 / 实现合约确定性部署的行为测试。

核心不变性：编译产物（artifacts/*.bin）+ 统一 salt + CREATE2 deployer 必须恰好
推导出 evm.constants 里的全网统一地址。这条断言把「本地部署地址」与「Python 侧
地址预测 / 生产部署脚本」锁死在同一值上，任何一端漂移都会被立即发现。
"""

from __future__ import annotations

from unittest.mock import Mock

from django.test import SimpleTestCase
from hexbytes import HexBytes
from web3 import Web3

from evm.constants import XCASH_VAULT_SLOT_FACTORY_ADDRESS
from evm.constants import XCASH_VAULT_SLOT_IMPLEMENTATION_ADDRESS
from evm.local_vault_slot import CREATE2_DEPLOYER_ADDRESS
from evm.local_vault_slot import XCASH_VAULT_SLOT_DEPLOY_SALT
from evm.local_vault_slot import build_implementation_init_code
from evm.local_vault_slot import build_factory_init_code
from evm.local_vault_slot import ensure_local_vault_slot_contracts
from evm.local_vault_slot import predict_create2_address


class VaultSlotAddressInvariantTests(SimpleTestCase):
    def test_artifacts_predict_expected_constant_addresses(self):
        """artifacts + salt 推导出的地址必须等于全网统一常量地址。"""
        implementation_address = predict_create2_address(build_implementation_init_code())
        self.assertEqual(
            implementation_address,
            Web3.to_checksum_address(XCASH_VAULT_SLOT_IMPLEMENTATION_ADDRESS),
        )

        # 工厂构造参数引用实现合约地址，实现合约漂移会连带让工厂地址漂移。
        factory_address = predict_create2_address(
            build_factory_init_code(implementation_address=implementation_address)
        )
        self.assertEqual(
            factory_address,
            Web3.to_checksum_address(XCASH_VAULT_SLOT_FACTORY_ADDRESS),
        )


class EnsureLocalVaultSlotContractsTests(SimpleTestCase):
    def _make_w3(self, *, deployer_present: bool = True) -> tuple[Mock, set, list]:
        """构造一个模拟 CREATE2 语义的 w3：send_transaction 把预测地址标记为已部署。"""
        deployer = Web3.to_checksum_address(CREATE2_DEPLOYER_ADDRESS)
        deployed: set[str] = set()
        if deployer_present:
            deployed.add(deployer)
        sent: list[dict] = []

        def get_code(address):
            return b"\x01" if Web3.to_checksum_address(address) in deployed else b""

        def send_transaction(tx):
            sent.append(tx)
            init_code = tx["data"][32:]  # 去掉前 32 字节 salt
            deployed.add(predict_create2_address(init_code))
            return HexBytes(b"\x11" * 32)

        w3 = Mock()
        w3.eth.get_code.side_effect = get_code
        w3.eth.accounts = ["0x0000000000000000000000000000000000000001"]
        w3.eth.send_transaction.side_effect = send_transaction
        w3.eth.wait_for_transaction_receipt.return_value = {"status": 1}
        return w3, deployed, sent

    def test_deploys_both_contracts_to_expected_addresses(self):
        w3, deployed, sent = self._make_w3()

        ensure_local_vault_slot_contracts(w3=w3)

        # 两笔部署交易都发往 CREATE2 deployer，且 data 以统一 salt 开头。
        self.assertEqual(len(sent), 2)
        for tx in sent:
            self.assertEqual(
                Web3.to_checksum_address(tx["to"]),
                Web3.to_checksum_address(CREATE2_DEPLOYER_ADDRESS),
            )
            self.assertTrue(tx["data"].startswith(XCASH_VAULT_SLOT_DEPLOY_SALT))
        self.assertIn(
            Web3.to_checksum_address(XCASH_VAULT_SLOT_IMPLEMENTATION_ADDRESS), deployed
        )
        self.assertIn(
            Web3.to_checksum_address(XCASH_VAULT_SLOT_FACTORY_ADDRESS), deployed
        )

    def test_idempotent_when_already_deployed(self):
        w3, deployed, sent = self._make_w3()
        deployed.add(Web3.to_checksum_address(XCASH_VAULT_SLOT_IMPLEMENTATION_ADDRESS))
        deployed.add(Web3.to_checksum_address(XCASH_VAULT_SLOT_FACTORY_ADDRESS))

        ensure_local_vault_slot_contracts(w3=w3)

        # 已部署：不再发任何部署交易。
        self.assertEqual(sent, [])

    def test_raises_when_create2_deployer_missing(self):
        w3, _deployed, sent = self._make_w3(deployer_present=False)

        with self.assertRaisesMessage(RuntimeError, "CREATE2 deployer"):
            ensure_local_vault_slot_contracts(w3=w3)
        self.assertEqual(sent, [])
