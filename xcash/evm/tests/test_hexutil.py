from django.test import SimpleTestCase
from hexbytes import HexBytes
from web3 import Web3

from evm.hexutil import checksum_address
from evm.hexutil import hex_lower
from evm.hexutil import parse_int
from evm.hexutil import parse_optional_int
from evm.hexutil import prefixed_hex
from evm.hexutil import required_hash
from evm.hexutil import topic_to_address


class EvmHexUtilTests(SimpleTestCase):
    """扫描入账以 (chain, hash, event_index) 去重、按 checksum 地址匹配，
    web3 返回 HexBytes 与节点返回字符串必须归一化成同一形式。"""

    def test_hash_forms_normalize_to_same_value(self):
        raw = "ab" * 32
        expected = f"0x{raw}"
        for value in (
            HexBytes(bytes.fromhex(raw)),
            f"0x{raw}",
            f"0X{raw.upper()}",
            raw,
        ):
            with self.subTest(value=value):
                self.assertEqual(prefixed_hex(value), expected)
                self.assertEqual(required_hash(value), expected)
                self.assertEqual(hex_lower(value), raw)

    def test_empty_hash_is_none_or_rejected(self):
        self.assertIsNone(prefixed_hex(None))
        self.assertIsNone(prefixed_hex(""))
        self.assertIsNone(prefixed_hex("0x"))
        with self.assertRaises(ValueError):
            required_hash("")

    def test_parse_int_accepts_int_hex_and_decimal(self):
        for value, expected in (
            (7, 7),
            ("0x1f", 31),
            ("0X1F", 31),
            (" 42 ", 42),
            ("", 0),
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_int(value), expected)
        self.assertIsNone(parse_optional_int(None))
        self.assertIsNone(parse_optional_int(""))
        self.assertEqual(parse_optional_int("0x2"), 2)

    def test_topic_to_address_takes_last_20_bytes_as_checksum(self):
        address = Web3.to_checksum_address("0x" + "ab" * 20)
        topic = "0x" + "00" * 12 + "ab" * 20

        self.assertEqual(topic_to_address(topic), address)
        self.assertEqual(topic_to_address(HexBytes(bytes.fromhex(topic[2:]))), address)

    def test_topic_too_short_or_invalid_address_raises(self):
        with self.assertRaises(ValueError):
            topic_to_address("0x1234")
        with self.assertRaises(ValueError):
            checksum_address("0xnot-an-address")
        # 非法输入不会被缓存成功结果：同一输入再次调用仍然报错。
        with self.assertRaises(ValueError):
            checksum_address("0xnot-an-address")
