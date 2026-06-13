// SPDX-License-Identifier: MIT
pragma solidity 0.8.35;

import {Test} from "forge-std/Test.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {XcashVaultSlot} from "../src/XcashVaultSlot.sol";
import {XcashVaultSlotFactory} from "../src/XcashVaultSlotFactory.sol";
import {MockERC20} from "./helpers/MockERC20.sol";

/// @notice VaultSlot 属性测试：单元测试用固定输入验证个别 case，这里让 Foundry
///         对金额、归集地址、salt 全空间随机取值，验证归集与地址预测的核心不变性
///         对任意输入都成立。
contract XcashVaultSlotFuzzTest is Test {
    address payable internal vault = payable(address(0xBEEF));
    XcashVaultSlotFactory internal factory;

    function setUp() public {
        XcashVaultSlot vaultSlotImplementation = new XcashVaultSlot();
        factory = new XcashVaultSlotFactory(address(vaultSlotImplementation));
    }

    /// 任意正数金额下，receive 必须把 msg.value 全额转给 vault，slot 不留存。
    function testFuzz_receive_forwards_any_native_amount(uint256 amount) public {
        amount = bound(amount, 1, type(uint128).max);
        XcashVaultSlot slot = _deployVaultSlot(vault, "fuzz-receive");
        address payer = address(0xA11CE);
        vm.deal(payer, amount);

        vm.prank(payer);
        (bool ok,) = address(slot).call{value: amount}("");

        assertTrue(ok);
        assertEq(vault.balance, amount);
        assertEq(address(slot).balance, 0);
    }

    /// 任意正数余额下，collect(native) 必须清空 slot 并全额转给 vault。
    function testFuzz_collect_native_sweeps_full_balance(uint256 amount) public {
        amount = bound(amount, 1, type(uint128).max);
        XcashVaultSlot slot = _deployVaultSlot(vault, "fuzz-collect-native");
        vm.deal(address(slot), amount);

        slot.collect(address(0));

        assertEq(vault.balance, amount);
        assertEq(address(slot).balance, 0);
    }

    /// 任意正数余额下，collect(erc20) 必须把全部代币转给 vault。
    function testFuzz_collect_erc20_sweeps_full_balance(uint256 amount) public {
        amount = bound(amount, 1, type(uint128).max);
        XcashVaultSlot slot = _deployVaultSlot(vault, "fuzz-collect-erc20");
        MockERC20 token = new MockERC20();
        token.mint(address(slot), amount);

        slot.collect(address(token));

        assertEq(token.balanceOf(vault), amount);
        assertEq(token.balanceOf(address(slot)), 0);
    }

    /// 资金只能流向 slot 自身编码的 immutable vault：对任意可收款的合法地址，
    /// 归集结果必须精确落到该地址，验证 immutable args 解码在全地址空间正确。
    function testFuzz_forwards_to_encoded_vault(address vaultArg, uint256 amount) public {
        assumeNotZeroAddress(vaultArg);
        assumeNotPrecompile(vaultArg);
        assumeNotForgeAddress(vaultArg);
        vm.assume(vaultArg.code.length == 0); // 无代码地址恒可收原生币，排除会 revert 的合约
        amount = bound(amount, 1, type(uint128).max);

        XcashVaultSlot slot = _deployVaultSlot(payable(vaultArg), "fuzz-any-vault");
        uint256 balanceBefore = vaultArg.balance;
        vm.deal(address(slot), amount);

        slot.collect(address(0));

        assertEq(vaultArg.balance, balanceBefore + amount);
        assertEq(address(slot).balance, 0);
    }

    /// 对任意 vault + salt，链上 CREATE2 部署地址必须等于 OZ Clones 预测公式的结果，
    /// 保证链下 contracts_codec.py 的地址预测与合约严格对齐。
    function testFuzz_predict_matches_deploy(address vaultArg, bytes32 salt) public {
        assumeNotZeroAddress(vaultArg);
        address predicted = _predict(payable(vaultArg), salt);

        address deployed = factory.deployVaultSlot(payable(vaultArg), salt);

        assertEq(deployed, predicted);
        assertGt(deployed.code.length, 0);
    }

    /// 同一 vault 下，任意两个不同 salt 必须派生出不同的 slot 地址。
    function testFuzz_distinct_salts_yield_distinct_slots(bytes32 saltA, bytes32 saltB)
        public
        view
    {
        vm.assume(saltA != saltB);

        address slotA = _predict(vault, saltA);
        address slotB = _predict(vault, saltB);

        assertNotEq(slotA, slotB);
    }

    function _deployVaultSlot(address payable vault_, string memory saltLabel)
        private
        returns (XcashVaultSlot)
    {
        return XcashVaultSlot(payable(factory.deployVaultSlot(vault_, keccak256(bytes(saltLabel)))));
    }

    function _predict(address payable vault_, bytes32 salt) private view returns (address) {
        return Clones.predictDeterministicAddressWithImmutableArgs(
            factory.vaultSlotImplementation(), abi.encodePacked(vault_), salt, address(factory)
        );
    }
}
