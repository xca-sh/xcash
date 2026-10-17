import common.fields
from django.db import migrations


def drop_index(name, columns):
    """删除 db_index 遗留的单列索引，反向按原定义重建。

    CONCURRENTLY 不拿 ACCESS EXCLUSIVE 锁，热表上删索引不阻塞读写；
    IF EXISTS / IF NOT EXISTS 让非原子迁移中途失败后可以直接重跑。
    """
    return migrations.RunSQL(
        sql=f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"',
        reverse_sql=(
            f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{name}" '
            f'ON "tron_trontxtask" ({columns})'
        ),
    )


class Migration(migrations.Migration):
    # DROP INDEX CONCURRENTLY 不能在事务内执行。
    atomic = False

    dependencies = [
        ("tron", "0001_initial"),
    ]

    operations = [
        # TronTxTask.to / tx_id 只在建任务、签名时写入，没有任何按这两列的查询。状态照常变更，
        # 数据库侧用 CONCURRENTLY 删除，避免 Django 默认的普通 DROP INDEX 在热表上拿排他锁。
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="trontxtask",
                    name="to",
                    field=common.fields.AddressField(max_length=100, verbose_name="To"),
                ),
                migrations.AlterField(
                    model_name="trontxtask",
                    name="tx_id",
                    field=common.fields.HashField(
                        blank=True,
                        max_length=100,
                        null=True,
                        unique=False,
                        verbose_name="当前 TxID",
                    ),
                ),
            ],
            database_operations=[
                drop_index("tron_trontxtask_to_6a91238d", '"to"'),
                drop_index(
                    "tron_trontxtask_to_6a91238d_like", '"to" varchar_pattern_ops'
                ),
                drop_index("tron_trontxtask_tx_id_85484e42", '"tx_id"'),
                drop_index(
                    "tron_trontxtask_tx_id_85484e42_like",
                    '"tx_id" varchar_pattern_ops',
                ),
            ],
        ),
    ]
