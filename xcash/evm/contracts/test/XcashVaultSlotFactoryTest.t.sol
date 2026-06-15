// SPDX-License-Identifier: MIT
pragma solidity 0.8.35;

import {Test} from "forge-std/Test.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {XcashVaultSlot} from "../src/XcashVaultSlot.sol";
import {XcashVaultSlotFactory} from "../src/XcashVaultSlotFactory.sol";
import {MockERC20} from "./helpers/MockERC20.sol";

contract XcashVaultSlotFactoryTest is Test {
    event XcashNativeReceived(address indexed from, uint256 amount);
    event XcashCollected(address indexed token, uint256 amount);
    event XcashVaultSlotDeployed(
        address indexed vaultSlot, address indexed vault, bytes32 indexed salt
    );

    address payable internal vault = payable(address(0xBEEF));
    address payable internal secondVault = payable(address(0xCAFE));
    XcashVaultSlot internal vaultSlotImplementation;
    XcashVaultSlotFactory internal factory;

    function setUp() public {
        vaultSlotImplementation = new XcashVaultSlot();
        factory = new XcashVaultSlotFactory(address(vaultSlotImplementation));
    }

    function test_reverts_when_vaultSlotImplementation_is_zero() public {
        vm.expectRevert(XcashVaultSlotFactory.InvalidVaultSlotImplementation.selector);
        new XcashVaultSlotFactory(address(0));
    }

    function test_reverts_when_vaultSlotImplementation_has_no_code() public {
        vm.expectRevert(XcashVaultSlotFactory.InvalidVaultSlotImplementation.selector);
        new XcashVaultSlotFactory(address(0x1234));
    }

    function test_reverts_when_vaultSlotImplementation_has_unexpected_codehash() public {
        MockERC20 wrongTemplate = new MockERC20();

        vm.expectRevert(XcashVaultSlotFactory.InvalidVaultSlotImplementation.selector);
        new XcashVaultSlotFactory(address(wrongTemplate));
    }

    function test_predict_address_matches_deployed_vault_slot() public {
        bytes32 salt = keccak256("deposit-001");
        address predicted = _predict(vault, salt);

        vm.expectEmit(true, true, true, true, address(factory));
        emit XcashVaultSlotDeployed(predicted, vault, salt);

        address deployed = factory.deployVaultSlot(vault, salt);

        assertEq(deployed, predicted);
        assertGt(deployed.code.length, 0);
    }

    function test_deployed_vault_slot_records_native_coin_from_vault_slot() public {
        bytes32 salt = keccak256("native-deposit");
        address payable predicted = payable(_predict(vault, salt));
        address payer = address(0xA11CE);
        vm.deal(payer, 1 ether);

        factory.deployVaultSlot(vault, salt);

        vm.expectEmit(true, true, true, true, predicted);
        emit XcashNativeReceived(payer, 1 ether);

        vm.prank(payer);
        (bool ok,) = predicted.call{value: 1 ether}("");

        assertTrue(ok);
        assertEq(vault.balance, 0);
        assertEq(predicted.balance, 1 ether);
    }

    function test_deployed_vault_slot_collects_erc20_to_vault() public {
        bytes32 salt = keccak256("erc20-deposit");
        address predicted = _predict(vault, salt);
        MockERC20 token = new MockERC20();
        token.mint(predicted, 1000e18);
        address deployed = factory.deployVaultSlot(vault, salt);

        vm.expectEmit(true, true, true, true, deployed);
        emit XcashCollected(address(token), 1000e18);

        XcashVaultSlot(payable(deployed)).collect(address(token));

        assertEq(token.balanceOf(vault), 1000e18);
        assertEq(token.balanceOf(deployed), 0);
    }

    function test_collect_on_funded_then_deployed_slot_sweeps_erc20() public {
        // 对应业务上的「先入金、后部署、再归集」两段式路径:
        // 资金先打到反事实地址,部署后由独立 collect 交易清扫。
        bytes32 salt = keccak256("fund-then-deploy");
        address predicted = _predict(vault, salt);
        MockERC20 token = new MockERC20();
        token.mint(predicted, 1000e18);

        address deployed = factory.deployVaultSlot(vault, salt);

        vm.expectEmit(true, true, true, true, deployed);
        emit XcashCollected(address(token), 1000e18);
        XcashVaultSlot(payable(deployed)).collect(address(token));

        assertEq(deployed, predicted);
        assertEq(token.balanceOf(vault), 1000e18);
        assertEq(token.balanceOf(deployed), 0);
    }

    function test_collect_on_funded_then_deployed_slot_sweeps_native_coin() public {
        // 对应「部署前原生币已打到 CREATE2 预测地址」的兜底路径:
        // 部署本身不触发 receive(),部署确认后由独立 collect(address(0)) 清扫。
        bytes32 salt = keccak256("fund-native-then-deploy");
        address predicted = _predict(vault, salt);
        vm.deal(predicted, 1.5 ether);

        address deployed = factory.deployVaultSlot(vault, salt);

        vm.expectEmit(true, true, true, true, deployed);
        emit XcashCollected(address(0), 1.5 ether);
        XcashVaultSlot(payable(deployed)).collect(address(0));

        assertEq(deployed, predicted);
        assertEq(vault.balance, 1.5 ether);
        assertEq(deployed.balance, 0);
    }

    function test_collect_native_sweeps_preexisting_and_received_balance() public {
        // 对应部署后系统已排 collect(address(0))，付款先到时仍由独立 collect 清扫全额余额。
        bytes32 salt = keccak256("receive-race-native");
        address payable predicted = payable(_predict(vault, salt));
        vm.deal(predicted, 0.4 ether);
        address payer = address(0xA11CE);
        vm.deal(payer, 0.6 ether);

        address deployed = factory.deployVaultSlot(vault, salt);

        vm.prank(payer);
        (bool ok,) = predicted.call{value: 0.6 ether}("");
        assertTrue(ok);
        assertEq(vault.balance, 0);
        assertEq(deployed.balance, 1 ether);

        vm.expectEmit(true, true, true, true, deployed);
        emit XcashCollected(address(0), 1 ether);
        XcashVaultSlot(payable(deployed)).collect(address(0));

        assertEq(vault.balance, 1 ether);
        assertEq(deployed.balance, 0);
    }

    function test_duplicate_salt_reverts() public {
        bytes32 salt = keccak256("duplicate");
        factory.deployVaultSlot(vault, salt);

        vm.expectRevert();
        factory.deployVaultSlot(vault, salt);
    }

    function test_same_salt_with_different_vaults_deploys_different_vault_slots() public {
        bytes32 salt = keccak256("shared-business-id");
        address firstPredicted = _predict(vault, salt);
        address secondPredicted = _predict(secondVault, salt);

        assertNotEq(firstPredicted, secondPredicted);

        address first = factory.deployVaultSlot(vault, salt);
        address second = factory.deployVaultSlot(secondVault, salt);

        assertEq(first, firstPredicted);
        assertEq(second, secondPredicted);
    }

    function test_deployed_vault_slot_keeps_native_coin_until_collect_to_its_own_vault_arg()
        public
    {
        bytes32 salt = keccak256("second-vault-native");
        address payable slot = payable(factory.deployVaultSlot(secondVault, salt));
        address payer = address(0xA11CE);
        vm.deal(payer, 1 ether);

        vm.prank(payer);
        (bool ok,) = slot.call{value: 1 ether}("");

        assertTrue(ok);
        assertEq(vault.balance, 0);
        assertEq(secondVault.balance, 0);
        assertEq(slot.balance, 1 ether);

        XcashVaultSlot(slot).collect(address(0));

        assertEq(vault.balance, 0);
        assertEq(secondVault.balance, 1 ether);
        assertEq(slot.balance, 0);
    }

    function _predict(address payable vault_, bytes32 salt) private view returns (address) {
        return Clones.predictDeterministicAddressWithImmutableArgs(
            address(vaultSlotImplementation), abi.encodePacked(vault_), salt, address(factory)
        );
    }
}
