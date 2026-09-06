import structlog
from celery import shared_task

from common.decorators import singleton_task
from config.worker_health import record_worker_health
from core.monitoring import OperationalRiskService

logger = structlog.get_logger()


@shared_task(bind=True, ignore_result=True, soft_time_limit=8, time_limit=10)
def report_business_worker_health(self):
    """只在业务 worker 实际取得执行容量后更新心跳。"""
    record_worker_health(self)


@shared_task(bind=True, ignore_result=True, soft_time_limit=8, time_limit=10)
def report_scan_worker_health(self):
    """没有活跃链时也能验证扫描 worker 的执行能力。"""
    record_worker_health(self)


@shared_task(ignore_result=True, soft_time_limit=100, time_limit=110)
@singleton_task(timeout=115)
def scan_operational_risks() -> None:
    """周期性巡检回调链路中的卡单风险，并输出结构化告警。"""
    summary = OperationalRiskService.build_summary(
        limit=3, include_resource_checks=True
    )
    # 每轮都刷新缓存（含清零），badge 等展示入口据此低成本判断资源风险，
    # 不必在页面渲染时实时打多链 RPC。须在下方 early-return 之前写入，否则
    # 风险消失（计数归零）那一轮不会更新缓存，badge 会卡在过期的风险态。
    OperationalRiskService.cache_resource_risk_counts(
        evm_low_native_balance_count=summary["evm_low_native_balance_count"],
        tron_low_resource_count=summary["tron_low_resource_count"],
    )
    risk_count = (
        summary["stalled_webhook_event_count"]
        + summary["evm_low_native_balance_count"]
        + summary["tron_low_resource_count"]
        + summary["stale_price_crypto_count"]
    )
    if not risk_count:
        return

    logger.warning(
        "运营巡检发现异常任务",
        stalled_webhook_events=summary["stalled_webhook_event_count"],
        evm_low_native_balances=summary["evm_low_native_balance_count"],
        tron_low_resources=summary["tron_low_resource_count"],
        # 价格陈旧会让账单按错误汇率收款，必须与卡单风险同级告警。
        stale_price_cryptos=summary["stale_price_crypto_count"],
        sample_stale_price_symbols=[
            item["symbol"] for item in summary["recent_stale_price_cryptos"]
        ],
        sample_event_ids=[
            event.pk for event in summary["recent_stalled_webhook_events"]
        ],
        sample_evm_senders=[
            alert["sender"].address
            for alert in summary["recent_evm_low_native_balance_alerts"]
            if alert.get("sender") is not None
        ],
        sample_tron_senders=[
            alert["sender"].address
            for alert in summary["recent_tron_low_resource_alerts"]
            if alert.get("sender") is not None
        ],
    )
