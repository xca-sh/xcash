// SPDX-License-Identifier: MIT
pragma solidity 0.8.35;

import {Test} from "forge-std/Test.sol";
import {XcashDepositTemplate} from "../src/XcashDepositTemplate.sol";
import {XcashDepositFactory} from "../src/XcashDepositFactory.sol";
import {MockERC20} from "./helpers/MockERC20.sol";
import {MockFalseReturnERC20} from "./helpers/MockFalseReturnERC20.sol";
import {MockMalformedReturnERC20} from "./helpers/MockMalformedReturnERC20.sol";
import {MockUsdtLike} from "./helpers/MockUsdtLike.sol";

contract XcashDepositSlotTest is Test {
    event XcashNativeDeposited(address indexed payer, uint256 amount);
    event XcashCollected(address indexed token, uint256 amount);

    address payable internal vault = payable(address(0xBEEF));
    XcashDepositFactory internal factory;

    function setUp() public {
        XcashDepositTemplate depositTemplate = new XcashDepositTemplate();
        factory = new XcashDepositFactory(address(depositTemplate));
    }

    function test_reverts_when_vault_is_zero() public {
        vm.expectRevert(XcashDepositFactory.ZeroVault.selector);
        factory.deployDepositSlot(payable(address(0)), keccak256("zero-vault"));
    }

    function test_receive_forwards_native_coin_to_vault_and_emits_event() public {
        XcashDepositTemplate depositSlot = _deployDepositSlot("receive-native");
        address payer = address(0xA11CE);
        vm.deal(payer, 2 ether);

        vm.expectEmit(true, true, true, true, address(depositSlot));
        emit XcashNativeDeposited(payer, 2 ether);

        vm.prank(payer);
        (bool ok,) = address(depositSlot).call{value: 2 ether}("");

        assertTrue(ok);
        assertEq(vault.balance, 2 ether);
        assertEq(address(depositSlot).balance, 0);
    }

    function test_receive_only_forwards_msg_value_leaving_preexisting_balance() public {
        XcashDepositTemplate depositSlot = _deployDepositSlot("existing-native");
        vm.deal(address(depositSlot), 0.4 ether);
        address payer = address(0xA11CE);
        vm.deal(payer, 0.6 ether);

        vm.expectEmit(true, true, true, true, address(depositSlot));
        emit XcashNativeDeposited(payer, 0.6 ether);

        vm.prank(payer);
        (bool ok,) = address(depositSlot).call{value: 0.6 ether}("");

        assertTrue(ok);
        // receive 只转出 msg.value，预存的 0.4 ether 留在 slot 等显式清扫。
        assertEq(vault.balance, 0.6 ether);
        assertEq(address(depositSlot).balance, 0.4 ether);

        vm.expectEmit(true, true, true, true, address(depositSlot));
        emit XcashCollected(address(0), 0.4 ether);
        depositSlot.collect(address(0));

        assertEq(vault.balance, 1 ether);
        assertEq(address(depositSlot).balance, 0);
    }

    function test_reverts_when_amount_is_zero() public {
        XcashDepositTemplate depositSlot = _deployDepositSlot("zero-amount");

        (bool ok, bytes memory data) = address(depositSlot).call{value: 0}("");

        assertFalse(ok);
        assertEq(data, abi.encodeWithSelector(XcashDepositTemplate.ZeroAmount.selector));
    }

    function test_reverts_when_vault_rejects_native_coin() public {
        RejectingVault rejectingVault = new RejectingVault();
        XcashDepositTemplate depositSlot =
            _deployDepositSlot(payable(address(rejectingVault)), "reject-native");

        (bool ok, bytes memory data) = address(depositSlot).call{value: 1 ether}("");

        assertFalse(ok);
        assertEq(data, abi.encodeWithSelector(XcashDepositTemplate.ForwardFailed.selector));
        assertEq(address(depositSlot).balance, 0);
        assertEq(address(rejectingVault).balance, 0);
    }

    function test_unknown_selector_with_value_reverts_without_fallback() public {
        // 移除 fallback() 后，带未知 selector 的调用必须直接 revert，资金不能被吞掉。
        XcashDepositTemplate depositSlot = _deployDepositSlot("no-fallback");
        address payer = address(0xCAFE);
        vm.deal(payer, 1 ether);

        vm.prank(payer);
        (bool ok,) = address(depositSlot).call{value: 1 ether}(hex"12345678");

        assertFalse(ok);
        assertEq(vault.balance, 0);
        assertEq(address(depositSlot).balance, 0);
        assertEq(payer.balance, 1 ether);
    }

    function test_collect_native_transfers_balance_to_vault() public {
        XcashDepositTemplate depositSlot = _deployDepositSlot("collect-native");
        vm.deal(address(depositSlot), 1.5 ether);

        vm.expectEmit(true, true, true, true, address(depositSlot));
        emit XcashCollected(address(0), 1.5 ether);

        depositSlot.collect(address(0));

        assertEq(vault.balance, 1.5 ether);
        assertEq(address(depositSlot).balance, 0);
    }

    function test_collect_native_reverts_when_balance_is_zero() public {
        XcashDepositTemplate depositSlot = _deployDepositSlot("collect-native-zero");

        vm.expectRevert(XcashDepositTemplate.ZeroAmount.selector);
        depositSlot.collect(address(0));
    }

    function test_collect_native_reverts_when_vault_rejects() public {
        RejectingVault rejectingVault = new RejectingVault();
        XcashDepositTemplate depositSlot =
            _deployDepositSlot(payable(address(rejectingVault)), "collect-native-reject");
        vm.deal(address(depositSlot), 1 ether);

        vm.expectRevert(XcashDepositTemplate.ForwardFailed.selector);
        depositSlot.collect(address(0));

        assertEq(address(depositSlot).balance, 1 ether);
        assertEq(address(rejectingVault).balance, 0);
    }

    function test_collect_erc20_transfers_full_balance_to_vault() public {
        XcashDepositTemplate depositSlot = _deployDepositSlot("erc20-standard");
        MockERC20 token = new MockERC20();
        token.mint(address(depositSlot), 1000e18);

        vm.expectEmit(true, true, true, true, address(depositSlot));
        emit XcashCollected(address(token), 1000e18);

        depositSlot.collect(address(token));

        assertEq(token.balanceOf(vault), 1000e18);
        assertEq(token.balanceOf(address(depositSlot)), 0);
    }

    function test_collect_erc20_supports_usdt_like_token() public {
        XcashDepositTemplate depositSlot = _deployDepositSlot("erc20-usdt-like");
        MockUsdtLike token = new MockUsdtLike();
        token.mint(address(depositSlot), 500e6);

        vm.expectEmit(true, true, true, true, address(depositSlot));
        emit XcashCollected(address(token), 500e6);

        depositSlot.collect(address(token));

        assertEq(token.balanceOf(vault), 500e6);
        assertEq(token.balanceOf(address(depositSlot)), 0);
    }

    function test_collect_erc20_reverts_when_balance_is_zero() public {
        XcashDepositTemplate depositSlot = _deployDepositSlot("erc20-zero");
        MockERC20 token = new MockERC20();

        vm.expectRevert(XcashDepositTemplate.ZeroAmount.selector);
        depositSlot.collect(address(token));
    }

    function test_collect_erc20_reverts_when_token_returns_false() public {
        XcashDepositTemplate depositSlot = _deployDepositSlot("erc20-false");
        MockFalseReturnERC20 token = new MockFalseReturnERC20();
        token.mint(address(depositSlot), 1);

        vm.expectRevert(XcashDepositTemplate.ERC20TransferFailed.selector);
        depositSlot.collect(address(token));
    }

    function test_collect_erc20_reverts_when_token_returns_malformed_bool() public {
        XcashDepositTemplate depositSlot = _deployDepositSlot("erc20-malformed");
        MockMalformedReturnERC20 token = new MockMalformedReturnERC20();
        token.mint(address(depositSlot), 1);

        vm.expectRevert(XcashDepositTemplate.ERC20TransferFailed.selector);
        depositSlot.collect(address(token));
    }

    function _deployDepositSlot(string memory saltLabel) private returns (XcashDepositTemplate) {
        return _deployDepositSlot(vault, saltLabel);
    }

    function _deployDepositSlot(address payable vault_, string memory saltLabel)
        private
        returns (XcashDepositTemplate)
    {
        return XcashDepositTemplate(
            payable(factory.deployDepositSlot(vault_, keccak256(bytes(saltLabel))))
        );
    }
}

contract RejectingVault {
    receive() external payable {
        revert("reject");
    }
}
