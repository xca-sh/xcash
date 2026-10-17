from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    # Invoice 是只增不减的大表：CONCURRENTLY 建索引不阻塞写入，但不能在事务内执行。
    atomic = False

    dependencies = [
        ("invoices", "0003_remove_risk_snapshot"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="invoice",
            index=models.Index(
                condition=models.Q(("status", "waiting"), ("transfer__isnull", True)),
                fields=["expires_at"],
                name="invoice_waiting_unpaid_idx",
            ),
        ),
    ]
