// SPDX-License-Identifier: MIT
pragma solidity 0.8.35;

import {Script, console} from "forge-std/Script.sol";
import {XcashVaultSlot} from "../src/XcashVaultSlot.sol";
import {XcashVaultSlotFactory} from "../src/XcashVaultSlotFactory.sol";

/// @title DeployXcashVaultSlot
/// @notice 通过 Foundry 默认的 Arachnid CREATE2 Deployer (0x4e59...4956C)
///         以确定性方式部署 XcashVaultSlot 与 XcashVaultSlotFactory。
///         任何链跑这个脚本得到的地址都必须等于下面的 EXPECTED_* 常量，
///         否则脚本 revert，避免地址漂移破坏「跨链同地址」假设。
contract DeployXcashVaultSlot is Script {
    /// @dev 全网统一 salt。语义化字符串便于未来 v2 迁移；一旦上线不可改。
    bytes32 internal constant DEPLOY_SALT = keccak256("xcash:evm-vault-slot:v1");

    /// @dev 全网期望地址。新链部署必须落到这两个地址，否则脚本 revert。
    ///      改动会同时打破 Python 侧 evm.constants 里的对应常量，必须同步评审。
    ///      地址依赖 foundry.toml 中 solc_version / optimizer_runs / via_ir /
    ///      evm_version / bytecode_hash / cbor_metadata 等编译参数，
    ///      以及合约源码本身——任何一项变动都会让 init_code 变、地址漂移。
    address internal constant EXPECTED_IMPLEMENTATION = 0x3E368358173C1621821C1Fe28f9f99F4E6126595;
    address internal constant EXPECTED_FACTORY = 0x5B77d455dF6543396A1292E5eC782D0BDa5a9b1D;

    function run() external {
        bytes32 implementationInitHash = keccak256(type(XcashVaultSlot).creationCode);
        address predictedImplementation =
            vm.computeCreate2Address(DEPLOY_SALT, implementationInitHash);

        bytes memory factoryInit = abi.encodePacked(
            type(XcashVaultSlotFactory).creationCode, abi.encode(predictedImplementation)
        );

        bytes32 factoryInitHash = keccak256(factoryInit);
        address predictedFactory = vm.computeCreate2Address(DEPLOY_SALT, factoryInitHash);

        console.log("Salt:");
        console.logBytes32(DEPLOY_SALT);
        console.log("Predicted implementation:", predictedImplementation);
        console.log("Predicted factory: ", predictedFactory);

        if (EXPECTED_IMPLEMENTATION != address(0)) {
            require(
                predictedImplementation == EXPECTED_IMPLEMENTATION, "implementation address drift"
            );
            require(predictedFactory == EXPECTED_FACTORY, "factory address drift");
        }

        vm.startBroadcast();
        XcashVaultSlot implementation = new XcashVaultSlot{salt: DEPLOY_SALT}();
        require(
            address(implementation) == predictedImplementation, "implementation deploy mismatch"
        );

        XcashVaultSlotFactory factory =
            new XcashVaultSlotFactory{salt: DEPLOY_SALT}(address(implementation));
        require(address(factory) == predictedFactory, "factory deploy mismatch");
        vm.stopBroadcast();

        console.log("Deployed implementation:", address(implementation));
        console.log("Deployed factory: ", address(factory));
    }
}
