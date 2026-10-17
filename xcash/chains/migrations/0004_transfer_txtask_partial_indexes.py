from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    # Transfer / TxTask 都是只增不减的大表：CONCURRENTLY 建索引不阻塞写入，
    # 但不能在事务内执行，故本迁移必须非原子。
    atomic = False

    dependencies = [
        ("chains", "0003_alter_vaultslotbalance_amount_and_more"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="transfer",
            index=models.Index(
                condition=models.Q(("processed_at__isnull", True)),
                fields=["created_at"],
                name="transfer_unprocessed_idx",
            ),
        ),
        AddIndexConcurrently(
            model_name="transfer",
            index=models.Index(
                condition=models.Q(("status", "confirming")),
                fields=["chain", "timestamp"],
                name="transfer_confirming_idx",
            ),
        ),
        AddIndexConcurrently(
            model_name="txtask",
            index=models.Index(
                condition=models.Q(("status__in", ("queued", "submitted"))),
                fields=["status"],
                name="txtask_active_status_idx",
            ),
        ),
    ]
