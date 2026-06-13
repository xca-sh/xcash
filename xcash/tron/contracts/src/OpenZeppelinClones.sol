// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @dev Minimal vendored subset of OpenZeppelin Contracts used by OpenZeppelinClones.
library Errors {
    error InsufficientBalance(uint256 balance, uint256 needed);
    error FailedDeployment();
}

/// @dev Minimal vendored subset of OpenZeppelin Contracts used by Create2.deploy.
library LowLevelCall {
    function returnDataSize() internal pure returns (uint256 size) {
        assembly ("memory-safe") {
            size := returndatasize()
        }
    }

    function bubbleRevert() internal pure {
        assembly ("memory-safe") {
            let fmp := mload(0x40)
            returndatacopy(fmp, 0x00, returndatasize())
            revert(fmp, returndatasize())
        }
    }
}

/// @dev Minimal vendored subset of OpenZeppelin Contracts Create2.
library Create2 {
    error Create2EmptyBytecode();

    function deploy(uint256 amount, bytes32 salt, bytes memory bytecode)
        internal
        returns (address addr)
    {
        if (address(this).balance < amount) {
            revert Errors.InsufficientBalance(address(this).balance, amount);
        }
        if (bytecode.length == 0) {
            revert Create2EmptyBytecode();
        }
        assembly ("memory-safe") {
            addr := create2(amount, add(bytecode, 0x20), mload(bytecode), salt)
        }
        if (addr == address(0)) {
            if (LowLevelCall.returnDataSize() == 0) {
                revert Errors.FailedDeployment();
            } else {
                LowLevelCall.bubbleRevert();
            }
        }
    }
}

/// @title OpenZeppelinClones
/// @notice Minimal vendored subset of OpenZeppelin Contracts Clones for Xcash Tron VaultSlot.
library OpenZeppelinClones {
    error CloneArgumentsTooLong();

    function cloneDeterministicWithImmutableArgs(
        address implementation,
        bytes memory args,
        bytes32 salt
    ) internal returns (address instance) {
        return cloneDeterministicWithImmutableArgs(implementation, args, salt, 0);
    }

    function cloneDeterministicWithImmutableArgs(
        address implementation,
        bytes memory args,
        bytes32 salt,
        uint256 value
    ) internal returns (address instance) {
        bytes memory bytecode = _cloneCodeWithImmutableArgs(implementation, args);
        return Create2.deploy(value, salt, bytecode);
    }

    function fetchCloneArgs(address instance) internal view returns (bytes memory) {
        bytes memory result = new bytes(instance.code.length - 0x2d);
        assembly ("memory-safe") {
            extcodecopy(instance, add(result, 0x20), 0x2d, mload(result))
        }
        return result;
    }

    function _cloneCodeWithImmutableArgs(address implementation, bytes memory args)
        private
        pure
        returns (bytes memory)
    {
        if (args.length > 0x5fd3) revert CloneArgumentsTooLong();
        return abi.encodePacked(
            hex"61",
            uint16(args.length + 0x2d),
            hex"3d81600a3d39f3363d3d373d3d3d363d73",
            implementation,
            hex"5af43d82803e903d91602b57fd5bf3",
            args
        );
    }
}
