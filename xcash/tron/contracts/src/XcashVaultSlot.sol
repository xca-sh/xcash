// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {OpenZeppelinClones} from "./OpenZeppelinClones.sol";

/// @title XcashVaultSlot
/// @notice Tron/TVM vault slot. TRX deposits are observed off-chain and swept explicitly.
contract XcashVaultSlot {
    error ZeroVault();
    error InvalidVaultArgs();
    error ForwardFailed();
    error ERC20TransferFailed();

    event XcashCollected(address indexed token, uint256 amount);

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
        bytes memory args = OpenZeppelinClones.fetchCloneArgs(address(this));
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
