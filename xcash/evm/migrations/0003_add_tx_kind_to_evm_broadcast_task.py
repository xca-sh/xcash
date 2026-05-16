from django.db import migrations
from django.db import models


def backfill_tx_kind(apps, schema_editor):
    EvmBroadcastTask = apps.get_model("evm", "EvmBroadcastTask")
    EvmBroadcastTask.objects.filter(
        models.Q(data="") | models.Q(data="0x")
    ).update(tx_kind="native_transfer")
    EvmBroadcastTask.objects.filter(tx_kind__isnull=True).update(
        tx_kind="contract_call"
    )


def noop_reverse(apps, schema_editor):
    """tx_kind 是前向迁移派生值；回滚时原始空值/异常值不可恢复。"""


class Migration(migrations.Migration):

    dependencies = [
        ("evm", "0002_alter_evmscancursor_last_error"),
    ]

    operations = [
        migrations.AddField(
            model_name="evmbroadcasttask",
            name="tx_kind",
            field=models.CharField(
                choices=[
                    ("native_transfer", "原生币转账"),
                    ("contract_call", "合约调用"),
                ],
                max_length=32,
                null=True,
                verbose_name="交易形态",
            ),
        ),
        migrations.RunPython(backfill_tx_kind, noop_reverse),
        migrations.AlterField(
            model_name="evmbroadcasttask",
            name="tx_kind",
            field=models.CharField(
                choices=[
                    ("native_transfer", "原生币转账"),
                    ("contract_call", "合约调用"),
                ],
                max_length=32,
                verbose_name="交易形态",
            ),
        ),
    ]
