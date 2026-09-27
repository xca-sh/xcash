from types import SimpleNamespace

from django.test import SimpleTestCase

from common.fields import AddressField
from common.fields import HashField
from common.tron_codec import TronAddressCodec


class TronAddressValidationTests(SimpleTestCase):
    def test_codec_base58_validation_requires_real_checksum(self):
        self.assertTrue(
            TronAddressCodec.is_valid_base58("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
        )
        self.assertFalse(
            TronAddressCodec.is_valid_base58("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6u")
        )

    def test_address_field_requires_real_tron_base58_checksum(self):
        field = AddressField()
        field.set_attributes_from_name("address")

        valid_instance = SimpleNamespace(address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
        invalid_instance = SimpleNamespace(address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6u")

        self.assertEqual(
            field.pre_save(valid_instance, add=True),
            "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        )
        with self.assertRaisesRegex(ValueError, "not a valid address"):
            field.pre_save(invalid_instance, add=True)


class HashFieldValidationTests(SimpleTestCase):
    def test_hash_field_accepts_supported_chain_hash_formats(self):
        field = HashField()
        field.set_attributes_from_name("hash")

        for value in (
            "0x" + "a" * 64,
            "b" * 64,
        ):
            with self.subTest(value=value):
                instance = SimpleNamespace(hash=value)
                self.assertEqual(field.pre_save(instance, add=True), value)
