from unittest.mock import Mock

from django.contrib.admin.sites import AdminSite
from django.test import SimpleTestCase

from evm.admin import EvmScanCursorAdmin
from evm.admin import EvmTxTaskAdmin
from evm.models import EvmScanCursor
from evm.models import EvmTxTask


class EvmTxTaskAdminTests(SimpleTestCase):
    def test_tx_task_admin_excludes_signed_payload(self):
        model_admin = EvmTxTaskAdmin(EvmTxTask, AdminSite())

        self.assertIn("signed_payload", model_admin.get_exclude(Mock(), obj=None))


class EvmScanCursorAdminTests(SimpleTestCase):
    def setUp(self):
        self.admin = EvmScanCursorAdmin(EvmScanCursor, AdminSite())

    def test_scan_cursor_admin_disallows_delete(self):
        self.assertIn("has_delete_permission", EvmScanCursorAdmin.__dict__)
        request = Mock()

        self.assertFalse(self.admin.has_delete_permission(request, obj=None))
        self.assertFalse(self.admin.has_delete_permission(request, obj=object()))
