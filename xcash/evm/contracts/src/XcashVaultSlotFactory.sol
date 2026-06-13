// SPDX-License-Identifier: MIT
pragma solidity 0.8.35;

import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {XcashVaultSlotTemplate} from "./XcashVaultSlotTemplate.sol";

/// @title XcashVaultSlotFactory
/// @notice Deploys XcashVaultSlot addresses with immutable vault args at deterministic CREATE2 addresses.
contract XcashVaultSlotFactory {
    error InvalidVaultSlotTemplate();
    error ZeroVault();

    event XcashVaultSlotDeployed(
        address indexed vaultSlot, address indexed vault, bytes32 indexed salt
    );

    address public immutable vaultSlotTemplate;

    constructor(address vaultSlotTemplate_) {
        if (vaultSlotTemplate_.codehash != keccak256(type(XcashVaultSlotTemplate).runtimeCode)) {
            revert InvalidVaultSlotTemplate();
        }
        vaultSlotTemplate = vaultSlotTemplate_;
    }

    function deployVaultSlot(address payable vault, bytes32 salt)
        external
        returns (address vaultSlot)
    {
        if (vault == address(0)) revert ZeroVault();
        vaultSlot = Clones.cloneDeterministicWithImmutableArgs(
            vaultSlotTemplate, abi.encodePacked(vault), salt
        );
        emit XcashVaultSlotDeployed(vaultSlot, vault, salt);
    }
}
