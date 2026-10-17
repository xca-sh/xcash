import common.fields
from django.db import migrations


class Migration(migrations.Migration):
    # DROP INDEX CONCURRENTLY 不能在事务内执行。
    atomic = False

    dependencies = [
        ("evm", "0001_initial"),
    ]

    operations = [
        # EvmTxTask.to 只在建任务时写入，没有任何按目标地址的查询。状态照常变更，数据库侧用
        # CONCURRENTLY 删除，避免 Django 默认的普通 DROP INDEX 在热表上拿排他锁。
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="evmtxtask",
                    name="to",
                    field=common.fields.EvmAddressField(
                        max_length=42, verbose_name="To"
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql='DROP INDEX CONCURRENTLY IF EXISTS "evm_evmtxtask_to_f4f31b27"',
                    reverse_sql=(
                        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                        '"evm_evmtxtask_to_f4f31b27" ON "evm_evmtxtask" ("to")'
                    ),
                ),
                migrations.RunSQL(
                    sql=(
                        "DROP INDEX CONCURRENTLY IF EXISTS "
                        '"evm_evmtxtask_to_f4f31b27_like"'
                    ),
                    reverse_sql=(
                        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                        '"evm_evmtxtask_to_f4f31b27_like" '
                        'ON "evm_evmtxtask" ("to" varchar_pattern_ops)'
                    ),
                ),
            ],
        ),
    ]
