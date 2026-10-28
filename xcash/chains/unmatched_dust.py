"""未归类粉尘转账的判定与定期清理。

扫描器会把发往监听地址的每一笔入账都落成 Transfer，其中大量是没有任何业务认领的
粉尘（地址投毒、广告转账等，常见 0.000001 TRX 级别），只会淹没后台列表。

未归类但有真实金额的入账（账单过期后才付款、付错金额等）是人工补单、客诉与对账的
唯一凭据，必须保留；因此清理只针对「价值低于粉尘阈值」的那一部分。
"""

from decimal import Decimal

import structlog
from django.db.models import Q

from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import TransferType
from common.time import ago
from currencies.models import Crypto
from currencies.models import PriceUnavailableError

logger = structlog.get_logger()

# 价值严格低于该美元金额的未归类入账视为粉尘。
UNMATCHED_DUST_MAX_WORTH_USD = Decimal("1")
# 粉尘只在确认后保留期满才删除：期内仍可用于排查，且远超链重组窗口。
UNMATCHED_DUST_RETENTION_DAYS = 7
# 分批删除，避免一次大 DELETE 长时间占锁（首次上线时存量可能很多）。
UNMATCHED_DUST_DELETE_BATCH_SIZE = 1000


def dust_amount_q() -> Q:
    """按各币种实时美元价构造「价值低于粉尘阈值」的查询条件。

    把美元阈值换算成每个币种的数量上限，让判定落在数据库里完成，后台筛选与清理任务
    共用同一判据。无价格（未接行情、价格过期、价格为 0）的币种无法判定价值，一律不算
    粉尘——宁可留着，也不能误删真实入账。
    """
    condition = Q(pk__in=[])
    for crypto in Crypto.objects.all():
        try:
            price = crypto.price("USD")
        except PriceUnavailableError:
            continue
        if price <= 0:
            continue
        condition |= Q(
            crypto_id=crypto.pk,
            amount__lt=UNMATCHED_DUST_MAX_WORTH_USD / price,
        )
    return condition


def unmatched_dust_q() -> Q:
    """未归类且价值低于粉尘阈值的转账。"""
    return Q(type=TransferType.Unmatched) & dust_amount_q()


def purge_unmatched_dust_transfers() -> int:
    """删除确认完成且超过保留期的未归类粉尘转账，返回删除条数。

    删除条件同时满足：
    - 类型为未归类：已认领到账单 / 充值的入账永不删除；
    - 状态为已确认且已处理：确认流程（标记 VaultSlot 收款、刷新余额快照）已走完，
      确认中 / 失败的记录交给确认与重组流程处理；
    - 创建超过保留期；
    - 按当前价格价值低于粉尘阈值。

    删除后唯一约束 (chain, hash, event_index) 随之释放：若日后把扫描位点回拨到保留期
    之前重扫，这些粉尘会被重建并再次判为未归类，不会产生业务副作用。
    """
    candidates = Transfer.objects.filter(
        unmatched_dust_q(),
        status=TransferStatus.CONFIRMED,
        processed_at__isnull=False,
        created_at__lte=ago(days=UNMATCHED_DUST_RETENTION_DAYS),
    )
    deleted_count = 0
    while True:
        batch_ids = list(
            candidates.order_by("pk").values_list("pk", flat=True)[
                :UNMATCHED_DUST_DELETE_BATCH_SIZE
            ]
        )
        if not batch_ids:
            break
        # 删除时重新带上全部条件：取批与删除之间若有记录被改动（如人工补单改了类型），
        # 不满足条件的行不会被误删。
        _, deleted_per_model = candidates.filter(pk__in=batch_ids).delete()
        deleted_count += deleted_per_model.get(Transfer._meta.label, 0)
        if len(batch_ids) < UNMATCHED_DUST_DELETE_BATCH_SIZE:
            break

    logger.info("清理未归类粉尘转账", deleted_count=deleted_count)
    return deleted_count
