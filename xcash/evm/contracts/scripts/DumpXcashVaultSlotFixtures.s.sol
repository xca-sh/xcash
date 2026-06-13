// SPDX-License-Identifier: MIT
pragma solidity 0.8.35;

import {Script} from "forge-std/Script.sol";
import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";

contract DumpXcashVaultSlotFixtures is Script {
    function run() external {
        address factory = 0xfAcadE0000000000000000000000000000000000;
        address vaultSlotImplementation = 0x1eA7090000000000000000000000000000000000;
        bytes32 salt = bytes32(uint256(0xC0DE));

        address vault1 = 0x4444444444444444444444444444444444444444;
        address vault2 = 0x5555555555555555555555555555555555555555;
        bytes memory vaultArgs1 = abi.encodePacked(vault1);
        bytes memory vaultArgs2 = abi.encodePacked(vault2);

        string memory firstCase = _caseJson(
            "xcash_vault_slot",
            factory,
            vaultSlotImplementation,
            vault1,
            vaultArgs1,
            Clones.predictDeterministicAddressWithImmutableArgs(
                vaultSlotImplementation, vaultArgs1, salt, factory
            )
        );
        string memory secondCase = _caseJson(
            "xcash_vault_slot_second_vault",
            factory,
            vaultSlotImplementation,
            vault2,
            vaultArgs2,
            Clones.predictDeterministicAddressWithImmutableArgs(
                vaultSlotImplementation, vaultArgs2, salt, factory
            )
        );

        vm.writeFile(
            "../tests/fixtures/xcash_vault_slot_fixtures.json",
            string.concat("{", '"salt":"', vm.toString(salt), '",', firstCase, ",", secondCase, "}")
        );
    }

    function _caseJson(
        string memory name,
        address factory,
        address vaultSlotImplementation,
        address vault,
        bytes memory vaultArgs,
        address predicted
    ) private pure returns (string memory) {
        return string.concat(
            '"',
            name,
            '":{"factory":"',
            vm.toString(factory),
            '","vault_slot_implementation":"',
            vm.toString(vaultSlotImplementation),
            '","vault":"',
            vm.toString(vault),
            '","slot_init_code":"',
            vm.toString(_slotInitCodeWithImmutableArgs(vaultSlotImplementation, vaultArgs)),
            '","predicted":"',
            vm.toString(predicted),
            '"}'
        );
    }

    function _slotInitCodeWithImmutableArgs(address vaultSlotImplementation, bytes memory args)
        private
        pure
        returns (bytes memory)
    {
        return abi.encodePacked(
            hex"61",
            uint16(args.length + 0x2d),
            hex"3d81600a3d39f3363d3d373d3d3d363d73",
            vaultSlotImplementation,
            hex"5af43d82803e903d91602b57fd5bf3",
            args
        );
    }
}
