"""chains/keys.py 的密钥学回归测试。

派生与签名必须与 bip_utils + eth_account 的标准行为逐字节一致；这里用固定助记词的
黄金向量锁定结果，任何改动派生路径/签名实现导致地址或交易字节变化都会被立刻发现。
"""

from __future__ import annotations

from chains.keys import decrypt_mnemonic
from chains.keys import derive_evm_address
from chains.keys import derive_evm_private_key
from chains.keys import encrypt_mnemonic
from chains.keys import generate_mnemonic
from chains.keys import sign_evm_transaction

# 固定测试助记词（BIP39 校验和有效），空 passphrase。
GOLDEN_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon agent"
)

# (bip44_account, address_index, EIP-55 地址, 私钥十六进制)
GOLDEN_DERIVATIONS = [
    (0, 0, "0x197A1bEE163923815Ba58EaD0F14B3Fcd8C5926d",
     "7baa95e968e65395b2b4cc341885bcfbc0d820571af180c65cc9c5019551c669"),
    (0, 1, "0xFaC7f183C69892E7379202C7E440d23b84d909bf",
     "12bbb3776db944eaf610e68d9f70416034caf0033ea0149bb135de6e129bddef"),
    (1, 0, "0xf9E2301a1C62C5B7Bcaf5f42EA5D436098BB99B1",
     "735c368912a68064d227aff1f00292910e5f31c8a2111e41d59aa8b33aa7c6c6"),
    (1, 5, "0x48C4401ce3cd5BfcEa3f671462A47963E767546A",
     "65a1afabaaf8cf1ebf32372d5b40e48774e4f71d4765cd69a54b0b1ca782ee88"),
]


def test_derive_evm_address_and_private_key_match_golden_vectors():
    for account, index, address, private_key in GOLDEN_DERIVATIONS:
        assert (
            derive_evm_address(
                mnemonic=GOLDEN_MNEMONIC,
                bip44_account=account,
                address_index=index,
            )
            == address
        )
        assert (
            derive_evm_private_key(
                mnemonic=GOLDEN_MNEMONIC,
                bip44_account=account,
                address_index=index,
            )
            == private_key
        )


def test_sign_evm_transaction_matches_golden_vector():
    tx_dict = {
        "chainId": 1,
        "nonce": 0,
        "from": "0x197A1bEE163923815Ba58EaD0F14B3Fcd8C5926d",
        "to": "0x000000000000000000000000000000000000dEaD",
        "value": 1000000000000000,
        "data": "0x",
        "gas": 21000,
        "gasPrice": 20000000000,
    }
    signed = sign_evm_transaction(
        private_key=GOLDEN_DERIVATIONS[0][3],
        tx_dict=tx_dict,
    )
    assert signed.tx_hash == (
        "0xa3ba4f5f8e6d27a5768764e38c49c92da7aa27fa739f2c37d292459560b1e5a1"
    )
    assert signed.raw_transaction == (
        "0xf86b808504a817c80082520894000000000000000000000000000000000000"
        "dead87038d7ea4c680008025a0631421196f03b2272bf7d8e39d4a6e8619af3"
        "324de9745a315364273992ab2c8a00a3f085bd84125d75a22d1c72181ee4572"
        "e9142da48af8badfaa40deb99b15a7"
    )


def test_mnemonic_cipher_round_trips_with_random_salt_and_nonce():
    # 每次加密用独立随机盐 + nonce，故同一明文两次密文不同，但都能解回原文。
    token_a = encrypt_mnemonic(GOLDEN_MNEMONIC)
    token_b = encrypt_mnemonic(GOLDEN_MNEMONIC)
    assert token_a != token_b
    assert decrypt_mnemonic(token_a) == GOLDEN_MNEMONIC
    assert decrypt_mnemonic(token_b) == GOLDEN_MNEMONIC


def test_generate_mnemonic_returns_24_words():
    assert len(generate_mnemonic().split()) == 24
