from django.db import migrations

# POL 正确的 CoinGecko 行情 slug；见 chains.constants.NATIVE_COIN_COINGECKO_IDS。
WRONG_POL_COINGECKO_IDS = ("polygon", "matic-network")
CORRECT_POL_COINGECKO_ID = "polygon-ecosystem-token"


def fix_pol_coingecko_id(apps, schema_editor):
    """把存量 POL 原生币的错误 CoinGecko slug 归一到 polygon-ecosystem-token。

    早期常量把 POL 的 slug 配成裸 "polygon"（非法 id）或历史上的 "matic-network"
    （MATIC→POL 迁移后停更返空报价），两者都让 POL 建成 active 却永远拉不到价。
    coingecko_id 只在 native_coin get_or_create 首建时写入，改常量不会自愈存量，
    故在此按确定性规则回填：

    - 处理判据：symbol=="POL" 且 coingecko_id 落在已知错误值集合内；其余不动。
    - 冲突处理：coingecko_id 有 UNIQUE 约束。若目标 slug 已被另一条 Crypto 占用
      （理论上不该发生），跳过该行不覆盖，避免撞唯一约束；由运维事后按日志排查。
    - 幂等：已是正确 slug 的行不在处理判据内，重复执行无副作用。
    """
    Crypto = apps.get_model("currencies", "Crypto")
    if Crypto.objects.filter(coingecko_id=CORRECT_POL_COINGECKO_ID).exclude(
        symbol="POL"
    ).exists():
        return
    Crypto.objects.filter(
        symbol="POL",
        coingecko_id__in=WRONG_POL_COINGECKO_IDS,
    ).update(coingecko_id=CORRECT_POL_COINGECKO_ID)


def reverse_noop(apps, schema_editor):
    """no-op：回滚只会把 slug 还原成已知会拉空价格的错误值，无还原价值。"""


class Migration(migrations.Migration):
    dependencies = [
        ("currencies", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(fix_pol_coingecko_id, reverse_noop),
    ]
