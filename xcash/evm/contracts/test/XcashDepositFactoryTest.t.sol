// SPDX-License-Identifier: MIT
pragma solidity 0.8.35;

import {Test} from "forge-std/Test.sol";
import {XcashDepositTemplate} from "../src/XcashDepositTemplate.sol";
import {XcashDepositFactory} from "../src/XcashDepositFactory.sol";
import {MockERC20} from "./helpers/MockERC20.sol";

contract XcashDepositFactoryTest is Test {
    event XcashNativeDeposited(address indexed payer, uint256 amount);
    event XcashCollected(address indexed token, uint256 amount);
    event XcashDepositSlotDeployed(
        address indexed depositSlot, address indexed vault, bytes32 indexed salt
    );

    address payable internal vault = payable(address(0xBEEF));
    address payable internal secondVault = payable(address(0xCAFE));
    XcashDepositTemplate internal depositTemplate;
    XcashDepositFactory internal factory;

    function setUp() public {
        depositTemplate = new XcashDepositTemplate();
        factory = new XcashDepositFactory(address(depositTemplate));
    }

    function test_reverts_when_depositTemplate_is_zero() public {
        vm.expectRevert(XcashDepositFactory.InvalidDepositTemplate.selector);
        new XcashDepositFactory(address(0));
    }

    function test_reverts_when_depositTemplate_has_no_code() public {
        vm.expectRevert(XcashDepositFactory.InvalidDepositTemplate.selector);
        new XcashDepositFactory(address(0x1234));
    }

    function test_reverts_when_depositTemplate_has_unexpected_codehash() public {
        MockERC20 wrongTemplate = new MockERC20();

        vm.expectRevert(XcashDepositFactory.InvalidDepositTemplate.selector);
        new XcashDepositFactory(address(wrongTemplate));
    }

    function test_factory_exposes_single_predict_deposit_slot_selector() public pure {
        bytes4 selector = XcashDepositFactory.predictDepositSlot.selector;

        assertEq(selector, bytes4(keccak256("predictDepositSlot(address,bytes32)")));
    }

    function test_predict_address_matches_deployed_deposit_slot() public {
        bytes32 salt = keccak256("deposit-001");
        address predicted = factory.predictDepositSlot(vault, salt);

        vm.expectEmit(true, true, true, true, address(factory));
        emit XcashDepositSlotDeployed(predicted, vault, salt);

        address deployed = factory.deployDepositSlot(vault, salt);

        assertEq(deployed, predicted);
        assertGt(deployed.code.length, 0);
    }

    function test_deployed_deposit_slot_forwards_native_coin_and_emits_from_deposit_slot() public {
        bytes32 salt = keccak256("native-deposit");
        address payable predicted = payable(factory.predictDepositSlot(vault, salt));
        address payer = address(0xA11CE);
        vm.deal(payer, 1 ether);

        factory.deployDepositSlot(vault, salt);

        vm.expectEmit(true, true, true, true, predicted);
        emit XcashNativeDeposited(payer, 1 ether);

        vm.prank(payer);
        (bool ok,) = predicted.call{value: 1 ether}("");

        assertTrue(ok);
        assertEq(vault.balance, 1 ether);
        assertEq(predicted.balance, 0);
    }

    function test_deployed_deposit_slot_collects_erc20_to_depositTemplate_vault() public {
        bytes32 salt = keccak256("erc20-deposit");
        address predicted = factory.predictDepositSlot(vault, salt);
        MockERC20 token = new MockERC20();
        token.mint(predicted, 1000e18);
        address deployed = factory.deployDepositSlot(vault, salt);

        vm.expectEmit(true, true, true, true, deployed);
        emit XcashCollected(address(token), 1000e18);

        XcashDepositTemplate(payable(deployed)).collect(address(token));

        assertEq(token.balanceOf(vault), 1000e18);
        assertEq(token.balanceOf(deployed), 0);
    }

    function test_duplicate_salt_reverts() public {
        bytes32 salt = keccak256("duplicate");
        factory.deployDepositSlot(vault, salt);

        vm.expectRevert();
        factory.deployDepositSlot(vault, salt);
    }

    function test_same_salt_with_different_vaults_deploys_different_deposit_slots() public {
        bytes32 salt = keccak256("shared-business-id");
        address firstPredicted = factory.predictDepositSlot(vault, salt);
        address secondPredicted = factory.predictDepositSlot(secondVault, salt);

        assertNotEq(firstPredicted, secondPredicted);

        address first = factory.deployDepositSlot(vault, salt);
        address second = factory.deployDepositSlot(secondVault, salt);

        assertEq(first, firstPredicted);
        assertEq(second, secondPredicted);
    }

    function test_deployed_deposit_slot_forwards_native_coin_to_its_own_vault_arg() public {
        bytes32 salt = keccak256("second-vault-native");
        address payable deposit = payable(factory.deployDepositSlot(secondVault, salt));
        address payer = address(0xA11CE);
        vm.deal(payer, 1 ether);

        vm.prank(payer);
        (bool ok,) = deposit.call{value: 1 ether}("");

        assertTrue(ok);
        assertEq(vault.balance, 0);
        assertEq(secondVault.balance, 1 ether);
    }
}
