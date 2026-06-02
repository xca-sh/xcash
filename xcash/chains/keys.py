"""chains 模块的密钥学核心：BIP39 助记词、BIP44 HD 派生、EVM 交易签名、助记词静态加密。

设计原则：
- 派生路径固定为 BIP44 m/44'/coin'/account'/0/index（EVM coin=60'），seed 不带 passphrase，
  与 bip_utils + eth_account 的标准行为一致（黄金向量见 chains/tests 中的 parity 用例）。
- 一律使用久经考验的库（bip_utils / eth_account / cryptography），绝不手搓密码学。
- 助记词在静态存储前用 AES-256-GCM 加密，密钥经 HKDF-SHA256 从配置的高熵主密钥派生；
  每条密文使用独立随机盐 + 独立随机 nonce。
- 敏感数据（助记词、私钥、签名 payload）只在内存中短暂存在，绝不写日志。
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from bip_utils import Bip39MnemonicGenerator
from bip_utils import Bip39SeedGenerator
from bip_utils import Bip39WordsNum
from bip_utils import Bip44
from bip_utils import Bip44Changes
from bip_utils import Bip44Coins
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from eth_account import Account

from chains.constants import ChainType

# AES-256-GCM 密文布局：base64( salt[16] || nonce[12] || ciphertext+tag )。
CIPHER_SALT_LEN = 16
CIPHER_NONCE_LEN = 12  # AES-GCM 标准 nonce 长度
CIPHER_KEY_LEN = 32  # AES-256
CIPHER_HKDF_INFO = b"xcash-wallet-mnemonic-v1"


@dataclass(frozen=True)
class EvmSignedPayload:
    """一笔已签名 EVM 交易的归一化结果（小写 0x 十六进制）。"""

    tx_hash: str
    raw_transaction: str


def normalize_hex(value: str) -> str:
    """统一为小写、带 0x 前缀的十六进制字符串。"""
    normalized = value if value.startswith("0x") else f"0x{value}"
    return normalized.lower()


def coin_for_chain_type(chain_type: str) -> Bip44Coins:
    """把链族标识映射到 bip_utils 的 BIP44 币种。新增链在此扩 case。"""
    if chain_type == ChainType.EVM:
        return Bip44Coins.ETHEREUM
    raise NotImplementedError(f"unsupported chain_type={chain_type}")


def generate_mnemonic() -> str:
    """生成 24 词（256 bit 熵）英文助记词，与行业标准（Ledger/Trezor）一致。"""
    return str(
        Bip39MnemonicGenerator().FromWordsNumber(Bip39WordsNum.WORDS_NUM_24)
    )


class MnemonicCipher:
    """助记词静态加密：AES-256-GCM，密钥经 HKDF-SHA256 从配置主密钥派生。

    主密钥来自 settings.WALLET_MNEMONIC_ENCRYPTION_KEY（init_env 生成为高熵随机串，
    生产环境拒绝空值），输入本就高熵，HKDF 是正确且快速的选择，无需慢哈希抗暴力。
    """

    def __init__(self, *, master_key: str | None = None) -> None:
        key = master_key if master_key is not None else settings.WALLET_MNEMONIC_ENCRYPTION_KEY
        if not key:
            raise RuntimeError(
                "WALLET_MNEMONIC_ENCRYPTION_KEY 未配置，无法加解密钱包助记词"
            )
        self.master_key = key.encode("utf-8")

    def derive_key(self, salt: bytes) -> bytes:
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=CIPHER_KEY_LEN,
            salt=salt,
            info=CIPHER_HKDF_INFO,
        )
        return hkdf.derive(self.master_key)

    def encrypt(self, plaintext: str) -> str:
        salt = os.urandom(CIPHER_SALT_LEN)
        nonce = os.urandom(CIPHER_NONCE_LEN)
        aesgcm = AESGCM(self.derive_key(salt))
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.b64encode(salt + nonce + ciphertext).decode("ascii")

    def decrypt(self, token: str) -> str:
        raw = base64.b64decode(token)
        if len(raw) < CIPHER_SALT_LEN + CIPHER_NONCE_LEN:
            raise ValueError("密文长度非法")
        salt = raw[:CIPHER_SALT_LEN]
        nonce = raw[CIPHER_SALT_LEN : CIPHER_SALT_LEN + CIPHER_NONCE_LEN]
        ciphertext = raw[CIPHER_SALT_LEN + CIPHER_NONCE_LEN :]
        aesgcm = AESGCM(self.derive_key(salt))
        return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")


def encrypt_mnemonic(mnemonic: str) -> str:
    """用配置主密钥加密助记词，返回可入库的密文。"""
    return MnemonicCipher().encrypt(mnemonic)


def decrypt_mnemonic(token: str) -> str:
    """解密入库的助记词密文。"""
    return MnemonicCipher().decrypt(token)


def _bip44_account_ctx(
    *,
    mnemonic: str,
    chain_type: str,
    bip44_account: int,
    address_index: int,
):
    """派生 HD 钱包的完整叶子节点。

    EVM: BIP44 路径 m/44'/60'/{bip44_account}'/0/{address_index}，seed 不带 passphrase。
    """
    seed_bytes = Bip39SeedGenerator(mnemonic).Generate()
    return (
        Bip44.FromSeed(seed_bytes, coin_for_chain_type(chain_type))
        .Purpose()
        .Coin()
        .Account(bip44_account)
        .Change(Bip44Changes.CHAIN_EXT)
        .AddressIndex(address_index)
    )


def derive_evm_address(
    *,
    mnemonic: str,
    bip44_account: int,
    address_index: int,
) -> str:
    """派生 EVM 链上的 EIP-55 校验和地址。"""
    return (
        _bip44_account_ctx(
            mnemonic=mnemonic,
            chain_type=ChainType.EVM,
            bip44_account=bip44_account,
            address_index=address_index,
        )
        .PublicKey()
        .ToAddress()
    )


def derive_evm_private_key(
    *,
    mnemonic: str,
    bip44_account: int,
    address_index: int,
) -> str:
    """派生 EVM 私钥（32 字节十六进制，无 0x 前缀）。仅供进程内签名使用，绝不出系统、不写日志。"""
    return (
        _bip44_account_ctx(
            mnemonic=mnemonic,
            chain_type=ChainType.EVM,
            bip44_account=bip44_account,
            address_index=address_index,
        )
        .PrivateKey()
        .Raw()
        .ToBytes()
        .hex()
    )


def sign_evm_transaction(*, private_key: str, tx_dict: dict) -> EvmSignedPayload:
    """用给定私钥对 legacy EIP-155 交易签名，返回归一化后的 tx_hash 与 raw_transaction。"""
    signed = Account.sign_transaction(tx_dict, private_key)
    return EvmSignedPayload(
        tx_hash=normalize_hex(signed.hash.hex()),
        raw_transaction=normalize_hex(signed.raw_transaction.hex()),
    )
