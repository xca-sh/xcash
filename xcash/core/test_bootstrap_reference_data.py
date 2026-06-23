from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase
from django.test import override_settings


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class ReferenceDataBootstrapCommandTests(SimpleTestCase):
    @patch("core.management.commands.ensure_default_reference_data.ensure_default_reference_data")
    def test_command_bootstraps_default_reference_data(self, bootstrap_mock):
        call_command("ensure_default_reference_data")

        bootstrap_mock.assert_called_once()
        self.assertEqual(bootstrap_mock.call_args.kwargs["using"], "default")

    @patch("core.management.commands.ensure_default_reference_data.ensure_default_reference_data")
    def test_command_accepts_database_alias(self, bootstrap_mock):
        call_command("ensure_default_reference_data", database="replica")

        bootstrap_mock.assert_called_once()
        self.assertEqual(bootstrap_mock.call_args.kwargs["using"], "replica")
