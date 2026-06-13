// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {XcashVaultSlotFactory} from "../src/XcashVaultSlotFactory.sol";
import {XcashVaultSlot} from "../src/XcashVaultSlot.sol";
import {MockERC20} from "./helpers/MockERC20.sol";

contract XcashVaultSlotTronTest is Test {
    event XcashCollected(address indexed token, uint256 amount);

    uint256 internal constant ONE_NATIVE = 1_000_000_000_000_000_000;
    uint256 internal constant ONE_AND_HALF_NATIVE = 1_500_000_000_000_000_000;

    address payable internal vault = payable(address(0xBEEF));
    XcashVaultSlot internal vaultSlotImplementation;
    XcashVaultSlotFactory internal factory;

    function setUp() public {
        vaultSlotImplementation = new XcashVaultSlot();
        factory = new XcashVaultSlotFactory(address(vaultSlotImplementation));
    }

    function test_direct_native_value_call_reverts_without_receive() public {
        address payable slot = payable(factory.deployVaultSlot(vault, keccak256("no-receive")));
        address payer = address(0xA11CE);
        vm.deal(payer, ONE_NATIVE);

        vm.prank(payer);
        (bool ok,) = slot.call{value: ONE_NATIVE}("");

        assertFalse(ok);
        assertEq(slot.balance, 0);
        assertEq(vault.balance, 0);
    }

    function test_collect_native_transfers_preexisting_balance_to_vault() public {
        address slot = factory.deployVaultSlot(vault, keccak256("collect-native"));
        vm.deal(slot, ONE_AND_HALF_NATIVE);

        vm.expectEmit(true, true, true, true, slot);
        emit XcashCollected(address(0), ONE_AND_HALF_NATIVE);

        XcashVaultSlot(payable(slot)).collect(address(0));

        assertEq(vault.balance, ONE_AND_HALF_NATIVE);
        assertEq(slot.balance, 0);
    }

    function test_collect_erc20_transfers_full_balance_to_vault() public {
        address slot = factory.deployVaultSlot(vault, keccak256("collect-erc20"));
        MockERC20 token = new MockERC20();
        token.mint(slot, 1000e18);

        vm.expectEmit(true, true, true, true, slot);
        emit XcashCollected(address(token), 1000e18);

        XcashVaultSlot(payable(slot)).collect(address(token));

        assertEq(token.balanceOf(vault), 1000e18);
        assertEq(token.balanceOf(slot), 0);
    }

    function test_predict_address_matches_deployed_vault_slot() public {
        bytes32 salt = keccak256("predict");
        address predicted = predictEvmCreate2VaultSlot(salt);

        address deployed = factory.deployVaultSlot(vault, salt);

        assertEq(deployed, predicted);
        assertGt(deployed.code.length, 0);
    }

    function predictEvmCreate2VaultSlot(bytes32 salt) internal view returns (address) {
        bytes memory initCode = abi.encodePacked(
            hex"61",
            uint16(0x2d + 20),
            hex"3d81600a3d39f3363d3d373d3d3d363d73",
            address(vaultSlotImplementation),
            hex"5af43d82803e903d91602b57fd5bf3",
            vault
        );
        bytes32 digest =
            keccak256(abi.encodePacked(bytes1(0xff), address(factory), salt, keccak256(initCode)));
        return address(uint160(uint256(digest)));
    }
}
