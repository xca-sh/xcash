import structlog
from celery import shared_task
from django.utils import timezone

from common import http_client
from currencies.models import Crypto
from currencies.models import Fiat

logger = structlog.get_logger()


@shared_task(
    ignore_result=True,
)
def refresh_crypto_prices():
    crypto_ids = list(
        # 无 coingecko_id 的币（未上 CoinGecko 的自定义代币）没有可刷新的行情源，跳过；
        # 它们只支持非支付资产流转，不进按法币计价的支付，无需价格。
        Crypto.objects.filter(active=True, coingecko_id__isnull=False).values_list(
            "coingecko_id",
            flat=True,
        )
    )

    if not crypto_ids:
        return

    fiat_codes = list(
        Fiat.objects.all().values_list(
            "code",
            flat=True,
        )
    )

    if not fiat_codes:
        return

    # 修复：拆分 f-string，避免语法错误阻断 Celery task 导入。
    api_url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={','.join(crypto_ids)}"
        f"&vs_currencies={','.join(code.lower() for code in fiat_codes)}"
    )
    try:
        response = http_client.get(api_url, timeout=8)
        response.raise_for_status()
        price_data = response.json()
    except Exception:
        # 外部价格源失败时仅记录日志，避免周期任务异常中断整个 worker。
        logger.exception("刷新加密货币价格失败")
        return

    for crypto_id in crypto_ids:
        crypto = Crypto.objects.get(coingecko_id=crypto_id)
        fresh_prices = {}
        for fiat_code in fiat_codes:
            price = price_data.get(crypto_id, {}).get(fiat_code.lower(), None)
            if price:
                fresh_prices[fiat_code] = price

        if not fresh_prices:
            # 本轮没拿到该币任何报价（行情源未覆盖或返回空），不推进时间戳，
            # 否则会把陈旧价格伪装成刚刷新过的，新鲜度校验形同虚设。
            logger.warning(
                "本轮未取得任何价格，保留原时间戳",
                crypto=crypto.symbol,
                coingecko_id=crypto_id,
            )
            continue

        # 价格表只保留本轮拿到的报价：所有法币共用一个更新时间，若保留本轮缺失法币
        # 的旧价，时间戳推进后旧价会被当成新鲜价格使用。缺失的法币按价格不可用处理。
        crypto.prices = fresh_prices
        # 时间戳与价格必须同批写入：它是判断价格是否陈旧的唯一依据。
        crypto.prices_updated_at = timezone.now()
        # 价格刷新只更新这两个字段，避免把其他字段旧值随任务回写。
        crypto.save(update_fields=["prices", "prices_updated_at"])
