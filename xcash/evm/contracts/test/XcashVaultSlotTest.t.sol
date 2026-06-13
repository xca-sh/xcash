// SPDX-License-Identifier: MIT
pragma solidity 0.8.35;

import {Test} from "forge-std/Test.sol";
import {XcashVaultSlot} from "../src/XcashVaultSlot.sol";
import {XcashVaultSlotFactory} from "../src/XcashVaultSlotFactory.sol";
import {MockERC20} from "./helpers/MockERC20.sol";
import {MockFalseReturnERC20} from "./helpers/MockFalseReturnERC20.sol";
import {MockMalformedReturnERC20} from "./helpers/MockMalformedReturnERC20.sol";
import {MockUsdtLike} from "./helpers/MockUsdtLike.sol";

contract XcashVaultSlotTest is Test {
    event XcashNativeReceived(address indexed from, uint256 amount);
    event XcashCollected(address indexed token, uint256 amount);

    address payable internal vault = payable(address(0xBEEF));
    XcashVaultSlotFactory internal factory;

    function setUp() public {
        XcashVaultSlot vaultSlotImplementation = new XcashVaultSlot();
        factory = new XcashVaultSlotFactory(address(vaultSlotImplementation));
    }

    function test_reverts_when_vault_is_zero() public {
        vm.expectRevert(XcashVaultSlotFactory.ZeroVault.selector);
        factory.deployVaultSlot(payable(address(0)), keccak256("zero-vault"));
    }

    function test_receive_forwards_native_coin_to_vault_and_emits_event() public {
        XcashVaultSlot slot = _deployVaultSlot("receive-native");
        address payer = address(0xA11CE);
        vm.deal(payer, 2 ether);

        vm.expectEmit(true, true, true, true, address(slot));
        emit XcashNativeReceived(payer, 2 ether);
        vm.expectEmit(true, true, true, true, address(slot));
        emit XcashCollected(address(0), 2 ether);

        vm.prank(payer);
        (bool ok,) = address(slot).call{value: 2 ether}("");

        assertTrue(ok);
        assertEq(vault.balance, 2 ether);
        assertEq(address(slot).balance, 0);
    }

    function test_receive_forwards_full_balance_including_preexisting_native() public {
        XcashVaultSlot slot = _deployVaultSlot("existing-native");
        vm.deal(address(slot), 0.4 ether);
        address payer = address(0xA11CE);
        vm.deal(payer, 0.6 ether);

        vm.expectEmit(true, true, true, true, address(slot));
        emit XcashNativeReceived(payer, 0.6 ether);
        vm.expectEmit(true, true, true, true, address(slot));
        emit XcashCollected(address(0), 1 ether);

        vm.prank(payer);
        (bool ok,) = address(slot).call{value: 0.6 ether}("");

        assertTrue(ok);
        assertEq(vault.balance, 1 ether);
        assertEq(address(slot).balance, 0);
    }

    function test_receive_noop_when_amount_is_zero() public {
        XcashVaultSlot slot = _deployVaultSlot("zero-amount");

        vm.recordLogs();
        (bool ok, bytes memory data) = address(slot).call{value: 0}("");

        assertTrue(ok);
        assertEq(data.length, 0);
        assertEq(vm.getRecordedLogs().length, 0);
        assertEq(vault.balance, 0);
        assertEq(address(slot).balance, 0);
    }

    function test_reverts_when_vault_rejects_native_coin() public {
        RejectingVault rejectingVault = new RejectingVault();
        XcashVaultSlot slot = _deployVaultSlot(payable(address(rejectingVault)), "reject-native");

        (bool ok, bytes memory data) = address(slot).call{value: 1 ether}("");

        assertFalse(ok);
        assertEq(data, abi.encodeWithSelector(XcashVaultSlot.ForwardFailed.selector));
        assertEq(address(slot).balance, 0);
        assertEq(address(rejectingVault).balance, 0);
    }

    function test_unknown_selector_with_value_reverts_without_fallback() public {
        // 移除 fallback() 后，带未知 selector 的调用必须直接 revert，资金不能被吞掉。
        XcashVaultSlot slot = _deployVaultSlot("no-fallback");
        address payer = address(0xCAFE);
        vm.deal(payer, 1 ether);

        vm.prank(payer);
        (bool ok,) = address(slot).call{value: 1 ether}(hex"12345678");

        assertFalse(ok);
        assertEq(vault.balance, 0);
        assertEq(address(slot).balance, 0);
        assertEq(payer.balance, 1 ether);
    }

    function test_collect_native_transfers_balance_to_vault() public {
        XcashVaultSlot slot = _deployVaultSlot("collect-native");
        vm.deal(address(slot), 1.5 ether);

        vm.expectEmit(true, true, true, true, address(slot));
        emit XcashCollected(address(0), 1.5 ether);

        slot.collect(address(0));

        assertEq(vault.balance, 1.5 ether);
        assertEq(address(slot).balance, 0);
    }

    function test_collect_native_noop_when_balance_is_zero() public {
        XcashVaultSlot slot = _deployVaultSlot("collect-native-zero");

        vm.recordLogs();
        slot.collect(address(0));

        assertEq(vm.getRecordedLogs().length, 0);
        assertEq(vault.balance, 0);
        assertEq(address(slot).balance, 0);
    }

    function test_collect_native_reverts_when_vault_rejects() public {
        RejectingVault rejectingVault = new RejectingVault();
        XcashVaultSlot slot =
            _deployVaultSlot(payable(address(rejectingVault)), "collect-native-reject");
        vm.deal(address(slot), 1 ether);

        vm.expectRevert(XcashVaultSlot.ForwardFailed.selector);
        slot.collect(address(0));

        assertEq(address(slot).balance, 1 ether);
        assertEq(address(rejectingVault).balance, 0);
    }

    function test_collect_erc20_transfers_full_balance_to_vault() public {
        XcashVaultSlot slot = _deployVaultSlot("erc20-standard");
        MockERC20 token = new MockERC20();
        token.mint(address(slot), 1000e18);

        vm.expectEmit(true, true, true, true, address(slot));
        emit XcashCollected(address(token), 1000e18);

        slot.collect(address(token));

        assertEq(token.balanceOf(vault), 1000e18);
        assertEq(token.balanceOf(address(slot)), 0);
    }

    function test_collect_erc20_supports_usdt_like_token() public {
        XcashVaultSlot slot = _deployVaultSlot("erc20-usdt-like");
        MockUsdtLike token = new MockUsdtLike();
        token.mint(address(slot), 500e6);

        vm.expectEmit(true, true, true, true, address(slot));
        emit XcashCollected(address(token), 500e6);

        slot.collect(address(token));

        assertEq(token.balanceOf(vault), 500e6);
        assertEq(token.balanceOf(address(slot)), 0);
    }

    function test_collect_erc20_noop_when_balance_is_zero() public {
        // 空收是良性幂等（前序归集已把余额扫空）：collect 必须安静收尾——不 revert、
        // 不 emit、余额保持 0，避免把"无事可做"误报成 FAILED 任务。
        XcashVaultSlot slot = _deployVaultSlot("erc20-zero");
        MockERC20 token = new MockERC20();

        vm.recordLogs();
        slot.collect(address(token));

        assertEq(vm.getRecordedLogs().length, 0);
        assertEq(token.balanceOf(address(slot)), 0);
        assertEq(token.balanceOf(vault), 0);
    }

    function test_collect_erc20_reverts_when_token_returns_false() public {
        XcashVaultSlot slot = _deployVaultSlot("erc20-false");
        MockFalseReturnERC20 token = new MockFalseReturnERC20();
        token.mint(address(slot), 1);

        vm.expectRevert(XcashVaultSlot.ERC20TransferFailed.selector);
        slot.collect(address(token));
    }

    function test_collect_erc20_reverts_when_token_returns_malformed_bool() public {
        XcashVaultSlot slot = _deployVaultSlot("erc20-malformed");
        MockMalformedReturnERC20 token = new MockMalformedReturnERC20();
        token.mint(address(slot), 1);

        vm.expectRevert(XcashVaultSlot.ERC20TransferFailed.selector);
        slot.collect(address(token));
    }

    function _deployVaultSlot(string memory saltLabel) private returns (XcashVaultSlot) {
        return _deployVaultSlot(vault, saltLabel);
    }

    function _deployVaultSlot(address payable vault_, string memory saltLabel)
        private
        returns (XcashVaultSlot)
    {
        return XcashVaultSlot(payable(factory.deployVaultSlot(vault_, keccak256(bytes(saltLabel)))));
    }
}

contract RejectingVault {
    receive() external payable {
        revert("reject");
    }
}
