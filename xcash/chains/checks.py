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


@register()
def chain_registry_check(app_configs=None, **_kwargs):
    """启动装配校验：每个链类型、每个业务归类都必须有上层 app 登记实现。

    chains 通过 chains.registry 调用链族实现与业务处理器（上层 app 在 ready() 里登记）。
    漏登记不会在导入期报错，只会在运行期表现为「扫描到入账却无法处理 / 无法确认」，
    因此在 check 阶段（migrate、部署前 check --deploy 都会触发）提前拦截。
    """
    from chains.constants import ChainType  # noqa: PLC0415
    from chains.models import TransferType  # noqa: PLC0415
    from chains.registry import get_chain_family  # noqa: PLC0415
    from chains.registry import registered_transfer_types  # noqa: PLC0415

    errors: list[Error] = []
    for chain_type in ChainType:
        try:
            get_chain_family(chain_type)
        except ValueError:
            errors.append(
                Error(
                    f"链类型 {chain_type.value!r} 没有登记链族实现。",
                    hint="在对应链族 app 的 AppConfig.ready() 中调用 register_chain_family。",
                    id="chains.E002",
                )
            )
    registered = registered_transfer_types()
    for transfer_type in TransferType:
        if transfer_type == TransferType.Unmatched or transfer_type in registered:
            continue
        errors.append(
            Error(
                f"入账归类 {transfer_type.value!r} 没有登记业务处理器。",
                hint="在对应业务 app 的 AppConfig.ready() 中调用 register_transfer_handler。",
                id="chains.E003",
            )
        )
    return errors
