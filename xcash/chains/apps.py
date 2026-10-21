from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ChainsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "chains"
    verbose_name = _("区块链")

    def ready(self) -> None:
        # 钱包助记词加密密钥的系统检查在 app ready 时注册，确保部署前 `manage.py check` 就能发现配置缺口。
        from chains import checks  # noqa: F401

        # 收款地址开户上报 SaaS 计费
        from chains import signals  # noqa: F401
