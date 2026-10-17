import common.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    # DROP INDEX CONCURRENTLY 不能在事务内执行。
    atomic = False

    dependencies = [
        ("invoices", "0004_invoice_waiting_unpaid_index"),
    ]

    operations = [
        # Invoice.pay_address 确有按地址查询（入账撮合、组合占用），保留 btree：原索引改名接管为
        # Meta.indexes 声明的 invoice_pay_address_idx，免去重建；只用 CONCURRENTLY 删掉
        # 无前缀匹配查询的 _like 索引，避免在热表上拿排他锁。
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="invoice",
                    name="pay_address",
                    field=common.fields.AddressField(
                        blank=True,
                        max_length=100,
                        null=True,
                        verbose_name="账单收款地址",
                    ),
                ),
                migrations.AddIndex(
                    model_name="invoice",
                    index=models.Index(
                        fields=["pay_address"], name="invoice_pay_address_idx"
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        'ALTER INDEX IF EXISTS "invoices_invoice_pay_address_26b9d0ce" '
                        'RENAME TO "invoice_pay_address_idx"'
                    ),
                    reverse_sql=(
                        'ALTER INDEX IF EXISTS "invoice_pay_address_idx" '
                        'RENAME TO "invoices_invoice_pay_address_26b9d0ce"'
                    ),
                ),
                migrations.RunSQL(
                    sql=(
                        "DROP INDEX CONCURRENTLY IF EXISTS "
                        '"invoices_invoice_pay_address_26b9d0ce_like"'
                    ),
                    reverse_sql=(
                        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                        '"invoices_invoice_pay_address_26b9d0ce_like" '
                        'ON "invoices_invoice" ("pay_address" varchar_pattern_ops)'
                    ),
                ),
            ],
        ),
        # DifferRecipientAddress.address 是 unique=True，唯一索引照旧；去掉 db_index 只改状态，
        # 数据库侧为 no-op。
        migrations.AlterField(
            model_name="differrecipientaddress",
            name="address",
            field=common.fields.AddressField(
                error_messages={"unique": "该地址已被添加为钱包直收地址，不能重复添加"},
                max_length=100,
                unique=True,
                verbose_name="地址",
            ),
        ),
    ]
