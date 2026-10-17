from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    # WebhookEvent 是只增不减的大表：CONCURRENTLY 建索引不阻塞写入，但不能在事务内执行。
    atomic = False

    dependencies = [
        ("webhooks", "0002_webhookevent_attempt_count"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="webhookevent",
            index=models.Index(
                condition=models.Q(("status", "pending")),
                fields=["created_at"],
                name="webhook_event_pending_idx",
            ),
        ),
    ]
