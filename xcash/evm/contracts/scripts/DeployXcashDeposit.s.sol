// SPDX-License-Identifier: MIT
pragma solidity 0.8.35;

import {Script, console} from "forge-std/Script.sol";
import {XcashDepositTemplate} from "../src/XcashDepositTemplate.sol";
import {XcashDepositFactory} from "../src/XcashDepositFactory.sol";

/// @title DeployXcashDeposit
/// @notice 通过 Foundry 默认的 Arachnid CREATE2 Deployer (0x4e59...4956C)
///         以确定性方式部署 XcashDepositTemplate 与 XcashDepositFactory。
///         任何链跑这个脚本得到的地址都必须等于下面的 EXPECTED_* 常量，
///         否则脚本 revert，避免地址漂移破坏「跨链同地址」假设。
contract DeployXcashDeposit is Script {
    /// @dev 全网统一 salt。语义化字符串便于未来 v2 迁移；一旦上线不可改。
    bytes32 internal constant DEPLOY_SALT = keccak256("xcash:deposit:v1");

    /// @dev 全网期望地址。新链部署必须落到这两个地址，否则脚本 revert。
    ///      改动会同时打破 Python 侧 evm.constants 里的对应常量，必须同步评审。
    ///      地址依赖 foundry.toml 中 solc_version / optimizer_runs / via_ir /
    ///      evm_version / bytecode_hash / cbor_metadata 等编译参数，
    ///      以及合约源码本身——任何一项变动都会让 init_code 变、地址漂移。
    address internal constant EXPECTED_TEMPLATE = 0x3e0Cdb17Bc3E22adF994AC8c0c36083B5f04C408;
    address internal constant EXPECTED_FACTORY = 0x4652Dc955698Fbe9AFcBBf910B3D30d8Df81bB01;

    function run() external {
        bytes32 templateInitHash = keccak256(type(XcashDepositTemplate).creationCode);
        address predictedTemplate = vm.computeCreate2Address(DEPLOY_SALT, templateInitHash);

        bytes memory factoryInit = abi.encodePacked(
            type(XcashDepositFactory).creationCode, abi.encode(predictedTemplate)
        );

        bytes32 factoryInitHash = keccak256(factoryInit);
        address predictedFactory = vm.computeCreate2Address(DEPLOY_SALT, factoryInitHash);

        console.log("Salt:");
        console.logBytes32(DEPLOY_SALT);
        console.log("Predicted template:", predictedTemplate);
        console.log("Predicted factory: ", predictedFactory);

        if (EXPECTED_TEMPLATE != address(0)) {
            require(predictedTemplate == EXPECTED_TEMPLATE, "template address drift");
            require(predictedFactory == EXPECTED_FACTORY, "factory address drift");
        }

        vm.startBroadcast();
        XcashDepositTemplate template = new XcashDepositTemplate{salt: DEPLOY_SALT}();
        require(address(template) == predictedTemplate, "template deploy mismatch");

        XcashDepositFactory factory = new XcashDepositFactory{salt: DEPLOY_SALT}(address(template));
        require(address(factory) == predictedFactory, "factory deploy mismatch");
        vm.stopBroadcast();

        console.log("Deployed template:", address(template));
        console.log("Deployed factory: ", address(factory));
    }
}
