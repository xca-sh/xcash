from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class InvoicesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "invoices"
    verbose_name = _("账单收款")

    def ready(self):
        from invoices import chain_hooks  # noqa: PLC0415

        chain_hooks.register()
