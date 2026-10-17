import common.fields
import django.db.models.deletion
from django.db import migrations, models


def drop_index(name, table, columns):
    """删除 db_index 遗留的单列索引，反向按原定义重建。

    CONCURRENTLY 不拿 ACCESS EXCLUSIVE 锁，热表上删索引不阻塞读写；
    IF EXISTS / IF NOT EXISTS 让非原子迁移中途失败后可以直接重跑。
    """
    return migrations.RunSQL(
        sql=f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"',
        reverse_sql=(
            f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{name}" ON "{table}" ({columns})'
        ),
    )


class Migration(migrations.Migration):
    # DROP INDEX CONCURRENTLY 不能在事务内执行。
    atomic = False

    dependencies = [
        ("chains", "0004_transfer_txtask_partial_indexes"),
    ]

    operations = [
        # Django 自己的 AlterField(db_index=False) 会用普通 DROP INDEX 拿表级排他锁，
        # 外键字段还会删掉再重建 FK 约束（整表校验）。这里拆开：状态照常变更，
        # 数据库侧只做 CONCURRENTLY 删除与索引改名。
        # 删除依据：全仓库没有按这些列单独查询的代码；按哈希的查询都带 chain，
        # 被 (chain, hash...) 唯一约束覆盖；外键 chain_id 是这些唯一约束的首列。
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="transfer",
                    name="block_hash",
                    field=common.fields.HashField(
                        max_length=100, unique=False, verbose_name="区块哈希"
                    ),
                ),
                migrations.AlterField(
                    model_name="transfer",
                    name="chain",
                    field=models.ForeignKey(
                        db_index=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="chains.chain",
                        verbose_name="链",
                    ),
                ),
                migrations.AlterField(
                    model_name="transfer",
                    name="from_address",
                    field=common.fields.AddressField(
                        max_length=100, verbose_name="发送地址"
                    ),
                ),
                migrations.AlterField(
                    model_name="transfer",
                    name="hash",
                    field=common.fields.HashField(
                        max_length=100, unique=False, verbose_name="哈希"
                    ),
                ),
                migrations.AlterField(
                    model_name="transfer",
                    name="to_address",
                    field=common.fields.AddressField(
                        max_length=100, verbose_name="目的地址"
                    ),
                ),
                migrations.AlterField(
                    model_name="txhash",
                    name="chain",
                    field=models.ForeignKey(
                        db_index=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="tx_hashes",
                        to="chains.chain",
                        verbose_name="链",
                    ),
                ),
                migrations.AlterField(
                    model_name="txhash",
                    name="hash",
                    field=common.fields.HashField(
                        max_length=100, unique=False, verbose_name="交易哈希"
                    ),
                ),
                migrations.AlterField(
                    model_name="txtask",
                    name="chain",
                    field=models.ForeignKey(
                        db_index=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="chains.chain",
                        verbose_name="链",
                    ),
                ),
                migrations.AlterField(
                    model_name="txtask",
                    name="tx_hash",
                    field=common.fields.HashField(
                        blank=True,
                        max_length=100,
                        null=True,
                        unique=False,
                        verbose_name="交易哈希",
                    ),
                ),
                migrations.AlterField(
                    model_name="vaultslot",
                    name="address",
                    field=common.fields.AddressField(
                        max_length=100, verbose_name="收款地址"
                    ),
                ),
                migrations.AlterField(
                    model_name="vaultslot",
                    name="usage",
                    field=models.CharField(
                        choices=[("deposit", "充值收款"), ("invoice", "账单收款")],
                        max_length=16,
                        verbose_name="用途",
                    ),
                ),
                migrations.AlterField(
                    model_name="vaultslotbalance",
                    name="last_tx_hash",
                    field=common.fields.HashField(
                        blank=True,
                        max_length=100,
                        null=True,
                        unique=False,
                        verbose_name="最近触发交易",
                    ),
                ),
                migrations.AddIndex(
                    model_name="vaultslot",
                    index=models.Index(
                        fields=["address"], name="vault_slot_address_idx"
                    ),
                ),
            ],
            database_operations=[
                drop_index(
                    "chains_transfer_block_hash_9b3f6337",
                    "chains_transfer",
                    '"block_hash"',
                ),
                drop_index(
                    "chains_transfer_block_hash_9b3f6337_like",
                    "chains_transfer",
                    '"block_hash" varchar_pattern_ops',
                ),
                drop_index(
                    "chains_transfer_chain_id_0eb9c693",
                    "chains_transfer",
                    '"chain_id"',
                ),
                drop_index(
                    "chains_transfer_from_address_b14fcca4",
                    "chains_transfer",
                    '"from_address"',
                ),
                drop_index(
                    "chains_transfer_from_address_b14fcca4_like",
                    "chains_transfer",
                    '"from_address" varchar_pattern_ops',
                ),
                drop_index(
                    "chains_transfer_hash_bbae9096",
                    "chains_transfer",
                    '"hash"',
                ),
                drop_index(
                    "chains_transfer_hash_bbae9096_like",
                    "chains_transfer",
                    '"hash" varchar_pattern_ops',
                ),
                drop_index(
                    "chains_transfer_to_address_7148d24c",
                    "chains_transfer",
                    '"to_address"',
                ),
                drop_index(
                    "chains_transfer_to_address_7148d24c_like",
                    "chains_transfer",
                    '"to_address" varchar_pattern_ops',
                ),
                drop_index(
                    "chains_txhash_chain_id_49b00ad1",
                    "chains_txhash",
                    '"chain_id"',
                ),
                drop_index(
                    "chains_txhash_hash_92338d12",
                    "chains_txhash",
                    '"hash"',
                ),
                drop_index(
                    "chains_txhash_hash_92338d12_like",
                    "chains_txhash",
                    '"hash" varchar_pattern_ops',
                ),
                drop_index(
                    "chains_txtask_chain_id_95ec9b6d",
                    "chains_txtask",
                    '"chain_id"',
                ),
                drop_index(
                    "chains_txtask_tx_hash_b2de2e49",
                    "chains_txtask",
                    '"tx_hash"',
                ),
                drop_index(
                    "chains_txtask_tx_hash_b2de2e49_like",
                    "chains_txtask",
                    '"tx_hash" varchar_pattern_ops',
                ),
                # VaultSlot.address 跨链识别要按 address 单列查：原 btree 改名接管为
                # Meta.indexes 声明的 vault_slot_address_idx，免去重建；只删 _like。
                migrations.RunSQL(
                    sql=(
                        'ALTER INDEX IF EXISTS "chains_vaultslot_address_52e1d4e8" '
                        'RENAME TO "vault_slot_address_idx"'
                    ),
                    reverse_sql=(
                        'ALTER INDEX IF EXISTS "vault_slot_address_idx" '
                        'RENAME TO "chains_vaultslot_address_52e1d4e8"'
                    ),
                ),
                drop_index(
                    "chains_vaultslot_address_52e1d4e8_like",
                    "chains_vaultslot",
                    '"address" varchar_pattern_ops',
                ),
                drop_index(
                    "chains_vaultslot_usage_501b780f",
                    "chains_vaultslot",
                    '"usage"',
                ),
                drop_index(
                    "chains_vaultslot_usage_501b780f_like",
                    "chains_vaultslot",
                    '"usage" varchar_pattern_ops',
                ),
                drop_index(
                    "chains_vaultslotbalance_last_tx_hash_f9dbf523",
                    "chains_vaultslotbalance",
                    '"last_tx_hash"',
                ),
                drop_index(
                    "chains_vaultslotbalance_last_tx_hash_f9dbf523_like",
                    "chains_vaultslotbalance",
                    '"last_tx_hash" varchar_pattern_ops',
                ),
            ],
        ),
        # Address.address 是 unique=True，唯一索引照旧；去掉 db_index 只改状态，数据库侧为 no-op。
        migrations.AlterField(
            model_name="address",
            name="address",
            field=common.fields.AddressField(
                max_length=100, unique=True, verbose_name="地址"
            ),
        ),
    ]
