from django.apps import AppConfig
from django.db.models.signals import post_migrate
from django.utils.translation import gettext_lazy as _


class EvmConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "evm"
    verbose_name = _("EVM")

    def ready(self):
        import evm.signals  # noqa: F401, PLC0415
        from evm import chain_hooks  # noqa: PLC0415

        chain_hooks.register()

        post_migrate.connect(
            _install_db_triggers_after_migrate,
            dispatch_uid="evm.install_db_triggers",
        )


_installed_aliases: set[str] = set()


def _install_db_triggers_after_migrate(sender, **kwargs) -> None:
    using = kwargs.get("using", "default")
    if using in _installed_aliases:
        return
    from django.db import connections  # noqa: PLC0415

    existing_tables = set(connections[using].introspection.table_names())
    if "evm_evmtxtask" not in existing_tables:
        return
    from evm.db_triggers import install_triggers  # noqa: PLC0415

    install_triggers(using=using)
    _installed_aliases.add(using)
