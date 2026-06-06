// SPDX-License-Identifier: MIT
pragma solidity 0.8.35;

import {Test} from "forge-std/Test.sol";
import {Vm} from "forge-std/Vm.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {XcashVaultSlotTemplate} from "../src/XcashVaultSlotTemplate.sol";
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
    XcashVaultSlotTemplate internal vaultSlotTemplate;
    XcashVaultSlotFactory internal factory;

    function setUp() public {
        vaultSlotTemplate = new XcashVaultSlotTemplate();
        factory = new XcashVaultSlotFactory(address(vaultSlotTemplate));
    }

    function test_reverts_when_vaultSlotTemplate_is_zero() public {
        vm.expectRevert(XcashVaultSlotFactory.InvalidVaultSlotTemplate.selector);
        new XcashVaultSlotFactory(address(0));
    }

    function test_reverts_when_vaultSlotTemplate_has_no_code() public {
        vm.expectRevert(XcashVaultSlotFactory.InvalidVaultSlotTemplate.selector);
        new XcashVaultSlotFactory(address(0x1234));
    }

    function test_reverts_when_vaultSlotTemplate_has_unexpected_codehash() public {
        MockERC20 wrongTemplate = new MockERC20();

        vm.expectRevert(XcashVaultSlotFactory.InvalidVaultSlotTemplate.selector);
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

    function test_deployed_vault_slot_forwards_native_coin_and_emits_from_vault_slot() public {
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
        assertEq(vault.balance, 1 ether);
        assertEq(predicted.balance, 0);
    }

    function test_deployed_vault_slot_collects_erc20_to_vault() public {
        bytes32 salt = keccak256("erc20-deposit");
        address predicted = _predict(vault, salt);
        MockERC20 token = new MockERC20();
        token.mint(predicted, 1000e18);
        address deployed = factory.deployVaultSlot(vault, salt);

        vm.expectEmit(true, true, true, true, deployed);
        emit XcashCollected(address(token), 1000e18);

        XcashVaultSlotTemplate(payable(deployed)).collect(address(token));

        assertEq(token.balanceOf(vault), 1000e18);
        assertEq(token.balanceOf(deployed), 0);
    }

    function test_ensure_deployed_and_collect_deploys_then_collects_erc20() public {
        bytes32 salt = keccak256("ensure-erc20");
        address predicted = _predict(vault, salt);
        MockERC20 token = new MockERC20();
        token.mint(predicted, 1000e18);

        vm.expectEmit(true, true, true, true, address(factory));
        emit XcashVaultSlotDeployed(predicted, vault, salt);
        vm.expectEmit(true, true, true, true, predicted);
        emit XcashCollected(address(token), 1000e18);

        address deployed = factory.ensureDeployedAndCollect(vault, salt, address(token));

        assertEq(deployed, predicted);
        assertGt(deployed.code.length, 0);
        assertEq(token.balanceOf(vault), 1000e18);
        assertEq(token.balanceOf(deployed), 0);
    }

    function test_ensure_deployed_and_collect_reuses_existing_slot() public {
        bytes32 salt = keccak256("ensure-existing");
        address deployed = factory.deployVaultSlot(vault, salt);
        MockERC20 token = new MockERC20();
        token.mint(deployed, 500e18);

        vm.recordLogs();
        address ensured = factory.ensureDeployedAndCollect(vault, salt, address(token));

        Vm.Log[] memory logs = vm.getRecordedLogs();
        assertEq(ensured, deployed);
        assertEq(logs.length, 1);
        assertEq(logs[0].emitter, deployed);
        assertEq(token.balanceOf(vault), 500e18);
        assertEq(token.balanceOf(deployed), 0);
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

    function test_deployed_vault_slot_forwards_native_coin_to_its_own_vault_arg() public {
        bytes32 salt = keccak256("second-vault-native");
        address payable slot = payable(factory.deployVaultSlot(secondVault, salt));
        address payer = address(0xA11CE);
        vm.deal(payer, 1 ether);

        vm.prank(payer);
        (bool ok,) = slot.call{value: 1 ether}("");

        assertTrue(ok);
        assertEq(vault.balance, 0);
        assertEq(secondVault.balance, 1 ether);
    }

    function _predict(address payable vault_, bytes32 salt) private view returns (address) {
        return Clones.predictDeterministicAddressWithImmutableArgs(
            address(vaultSlotTemplate), abi.encodePacked(vault_), salt, address(factory)
        );
    }
}
