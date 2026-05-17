from django.db import transaction
from django.db.models.signals import post_save
from django.db.models.signals import pre_save
from django.dispatch import receiver

from chains.models import Chain
from currencies.models import ChainToken


@receiver(post_save, sender=Chain)
def ensure_native_crypto_mapping_for_chain(
    sender,
    instance: Chain,
    *,
    created: bool,
    **kwargs,
):
    # 新链落库后必须立即补齐原生币部署记录，保证 support_this_chain 等逻辑统一可用。
    if not created:
        return

    ChainToken.objects.get_or_create(
        crypto=instance.native_coin,
        chain=instance,
        defaults={"address": ""},
    )


@receiver(pre_save, sender=ChainToken)
def remember_old_crypto_on_chain_mapping(sender, instance: ChainToken, **kwargs):
    # 记录修改前的 crypto_id，供 post_save 判断是否发生了真实映射切换。
    if not instance.pk:
        instance._old_crypto_id = None
        return

    try:
        instance._old_crypto_id = (
            ChainToken.objects.only("crypto_id").get(pk=instance.pk).crypto_id
        )
    except ChainToken.DoesNotExist:
        instance._old_crypto_id = None


@receiver(post_save, sender=ChainToken)
def sync_transfers_when_chain_mapping_changes(
    sender,
    instance: ChainToken,
    *,
    created: bool,
    **kwargs,
):
    # 当管理员把占位映射改到正式 Crypto 时，历史 OnchainTransfer 要自动跟着修正并重归类。
    if created:
        return

    old_crypto_id = getattr(instance, "_old_crypto_id", None)
    if not old_crypto_id or old_crypto_id == instance.crypto_id:
        return

    from chains.models import OnchainTransfer
    from chains.tasks import process_transfer

    affected_transfer_ids = list(
        OnchainTransfer.objects.filter(
            chain=instance.chain,
            crypto_id=old_crypto_id,
        ).values_list("pk", flat=True)
    )
    if not affected_transfer_ids:
        return

    # 先统一改写历史 OnchainTransfer.crypto，保证展示和后续匹配都以新映射为准。
    OnchainTransfer.objects.filter(pk__in=affected_transfer_ids).update(
        crypto_id=instance.crypto_id
    )

    rematch_transfer_ids = list(
        OnchainTransfer.objects.filter(
            pk__in=affected_transfer_ids,
            type="",  # 未归类的 OnchainTransfer，type 默认为空字符串
        ).values_list("pk", flat=True)
    )
    if not rematch_transfer_ids:
        return

    # 未归类的历史转账需要重新跑 process()，让 Deposit 等业务对象自动补齐。
    OnchainTransfer.objects.filter(pk__in=rematch_transfer_ids).update(
        processed_at=None
    )
    for transfer_id in rematch_transfer_ids:
        transaction.on_commit(
            lambda transfer_id=transfer_id: process_transfer.delay(transfer_id)
        )
