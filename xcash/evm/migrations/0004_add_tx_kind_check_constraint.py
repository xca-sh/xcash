from django.db import migrations
from django.db import models


def normalize_tx_kind(apps, schema_editor):
    EvmBroadcastTask = apps.get_model("evm", "EvmBroadcastTask")
    valid_tx_kinds = ["native_transfer", "contract_call"]
    EvmBroadcastTask.objects.exclude(tx_kind__in=valid_tx_kinds).filter(
        models.Q(data="") | models.Q(data="0x")
    ).update(tx_kind="native_transfer")
    # 显式重新查询：上一步已修复的 native_transfer 行不应再被改写为 contract_call。
    EvmBroadcastTask.objects.exclude(tx_kind__in=valid_tx_kinds).update(
        tx_kind="contract_call"
    )


def noop_reverse(apps, schema_editor):
    """tx_kind 归一化会覆盖异常值；回滚时原始异常值不可恢复。"""


class Migration(migrations.Migration):

    dependencies = [
        ("evm", "0003_add_tx_kind_to_evm_broadcast_task"),
    ]

    operations = [
        migrations.RunPython(normalize_tx_kind, noop_reverse),
        migrations.AddConstraint(
            model_name="evmbroadcasttask",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("tx_kind__in", ["native_transfer", "contract_call"])
                ),
                name="ck_evm_broadcast_task_tx_kind_valid",
            ),
        ),
    ]
