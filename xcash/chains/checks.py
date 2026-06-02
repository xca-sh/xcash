from __future__ import annotations

from django.conf import settings
from django.core.checks import Error
from django.core.checks import register


@register()
def wallet_mnemonic_key_check(app_configs=None, **_kwargs):
    """部署前校验钱包助记词加密密钥已配置。

    钱包助记词以 AES-256-GCM 静态加密入库（见 chains/keys.py），密钥来自
    WALLET_MNEMONIC_ENCRYPTION_KEY。生产环境缺失即拒绝启动，避免无密钥时
    钱包生成直接抛错或退化到不安全状态。DEBUG 下允许使用本地默认密钥。
    """
    errors: list[Error] = []
    if not settings.DEBUG and not settings.WALLET_MNEMONIC_ENCRYPTION_KEY:
        errors.append(
            Error(
                "生产环境必须配置 WALLET_MNEMONIC_ENCRYPTION_KEY，"
                "否则无法加密保存钱包助记词。",
                id="chains.E001",
            )
        )
    return errors
