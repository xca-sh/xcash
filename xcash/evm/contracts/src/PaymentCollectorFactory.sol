// SPDX-License-Identifier: MIT
pragma solidity 0.8.35;

contract PaymentCollectorFactory {
    error DeployFailed();

    function deploy(bytes32 salt, bytes calldata initCode) external returns (address collector) {
        assembly {
            let ptr := mload(0x40)
            calldatacopy(ptr, initCode.offset, initCode.length)
            collector := create2(0, ptr, initCode.length, salt)
        }
        if (collector == address(0)) revert DeployFailed();
    }
}
