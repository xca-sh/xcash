// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {OpenZeppelinClones} from "./OpenZeppelinClones.sol";
import {XcashVaultSlot} from "./XcashVaultSlot.sol";

/// @title XcashVaultSlotFactory
/// @notice Deploys Tron XcashVaultSlot addresses with immutable vault args.
contract XcashVaultSlotFactory {
    error InvalidVaultSlotImplementation();
    error ZeroVault();

    event XcashVaultSlotDeployed(
        address indexed vaultSlot, address indexed vault, bytes32 indexed salt
    );

    address public immutable vaultSlotImplementation;

    constructor(address vaultSlotImplementation_) {
        if (vaultSlotImplementation_.codehash != keccak256(type(XcashVaultSlot).runtimeCode)) {
            revert InvalidVaultSlotImplementation();
        }
        vaultSlotImplementation = vaultSlotImplementation_;
    }

    function deployVaultSlot(address payable vault, bytes32 salt)
        external
        returns (address vaultSlot)
    {
        if (vault == address(0)) revert ZeroVault();

        vaultSlot = OpenZeppelinClones.cloneDeterministicWithImmutableArgs(
            vaultSlotImplementation, abi.encodePacked(vault), salt
        );
        emit XcashVaultSlotDeployed(vaultSlot, vault, salt);
    }
}
