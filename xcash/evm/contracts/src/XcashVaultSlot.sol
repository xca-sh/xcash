// SPDX-License-Identifier: MIT
pragma solidity 0.8.35;

import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";

/// @title XcashVaultSlot
/// @notice Native coin and ERC20 vault slot that forwards funds to its slot-encoded vault.
contract XcashVaultSlot {
    error ZeroVault();
    error InvalidVaultArgs();
    error ForwardFailed();
    error ERC20TransferFailed();

    event XcashNativeReceived(address indexed from, uint256 amount);
    event XcashCollected(address indexed token, uint256 amount);

    receive() external payable {
        if (msg.value == 0) return;
        emit XcashNativeReceived(msg.sender, msg.value);

        uint256 amount = address(this).balance;
        emit XcashCollected(address(0), amount);

        (bool ok,) = vault().call{value: amount}("");
        if (!ok) revert ForwardFailed();
    }

    function collect(address token) external {
        if (token == address(0)) {
            uint256 amount = address(this).balance;
            if (amount == 0) return;
            emit XcashCollected(address(0), amount);
            (bool ok,) = vault().call{value: amount}("");
            if (!ok) revert ForwardFailed();
        } else {
            _collectERC20(token);
        }
    }

    function _collectERC20(address token) private {
        uint256 amount = IERC20BalanceOf(token).balanceOf(address(this));
        if (amount == 0) return;
        address payable vault_ = vault();

        (bool ok,) = token.call(abi.encodeCall(IERC20Transfer.transfer, (vault_, amount)));
        if (!ok) revert ERC20TransferFailed();
        if (IERC20BalanceOf(token).balanceOf(address(this)) != 0) {
            revert ERC20TransferFailed();
        }

        emit XcashCollected(token, amount);
    }

    function vault() public view returns (address payable vault_) {
        bytes memory args = Clones.fetchCloneArgs(address(this));
        if (args.length != 20) revert InvalidVaultArgs();

        uint160 rawVault;
        assembly ("memory-safe") {
            rawVault := shr(96, mload(add(args, 32)))
        }
        vault_ = payable(address(rawVault));
        if (vault_ == address(0)) revert ZeroVault();
    }
}

interface IERC20BalanceOf {
    function balanceOf(address account) external view returns (uint256);
}

interface IERC20Transfer {
    function transfer(address to, uint256 amount) external returns (bool);
}
