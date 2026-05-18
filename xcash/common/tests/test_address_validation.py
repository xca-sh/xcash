from types import SimpleNamespace

from django.test import SimpleTestCase
from tron.codec import TronAddressCodec

from common.fields import AddressField


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

        valid_instance = SimpleNamespace(
            address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
        )
        invalid_instance = SimpleNamespace(
            address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6u"
        )

        self.assertEqual(
            field.pre_save(valid_instance, add=True),
            "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        )
        with self.assertRaisesRegex(ValueError, "not a valid address"):
            field.pre_save(invalid_instance, add=True)
