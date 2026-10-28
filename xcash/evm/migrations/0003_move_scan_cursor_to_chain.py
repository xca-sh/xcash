from django.db import migrations


def copy_cursor_to_chain(apps, schema_editor):
    """把 EvmScanCursor 的扫描位点逐行拷到所属 Chain 的 scan_* 字段。

    归一化规则：EvmScanCursor 按 chain 唯一，一行对应一条链，字段一一拷贝
    （last_scanned_block→scan_cursor_block、enabled→scan_enabled、
    last_error→scan_last_error、last_error_at→scan_last_error_at），不存在冲突。
    没有游标的链保持新列默认值（位点 0、启用），与旧扫描器首轮 get_or_create 建出的
    初值相同。按游标覆盖写入，重复执行结果不变。

    位点必须原样搬过去：位点为 0 会让扫描器把起点重新锚到链头，静默跳过停服期间的区块。
    """
    Chain = apps.get_model("chains", "Chain")
    Cursor = apps.get_model("evm", "EvmScanCursor")
    for cursor in Cursor.objects.all().iterator():
        Chain.objects.filter(pk=cursor.chain_id).update(
            scan_cursor_block=cursor.last_scanned_block,
            scan_enabled=cursor.enabled,
            scan_last_error=cursor.last_error,
            scan_last_error_at=cursor.last_error_at,
        )


def copy_chain_to_cursor(apps, schema_editor):
    """回滚时按 Chain 的 scan_* 字段为每条 EVM 链重建 EvmScanCursor。

    正向迁移前没有游标的链会多出一行位点为 0 的游标，与旧扫描器首轮 get_or_create
    建出的初值相同，行为等价。
    """
    Chain = apps.get_model("chains", "Chain")
    Cursor = apps.get_model("evm", "EvmScanCursor")
    for chain in Chain.objects.filter(type="evm").iterator():
        Cursor.objects.update_or_create(
            chain=chain,
            defaults={
                "last_scanned_block": chain.scan_cursor_block,
                "enabled": chain.scan_enabled,
                "last_error": chain.scan_last_error,
                "last_error_at": chain.scan_last_error_at,
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("evm", "0002_drop_redundant_indexes"),
        ("chains", "0006_chain_scan_cursor_fields"),
    ]

    # 扫描位点并入 Chain：先用 RunPython 把位点原样拷到 Chain.scan_*，再删游标表。
    # upgrade.sh 在 production migrate 前停掉全部业务服务，不存在旧代码继续读写游标表的窗口。
    operations = [
        migrations.RunPython(copy_cursor_to_chain, copy_chain_to_cursor),
        migrations.DeleteModel(name="EvmScanCursor"),
    ]
