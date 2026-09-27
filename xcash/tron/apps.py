from django.apps import AppConfig


class TronConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "tron"
    verbose_name = "Tron"

    def ready(self):
        from tron import chain_hooks  # noqa: PLC0415

        chain_hooks.register()
