import json

from django.conf import settings
from django.contrib import admin
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import RedirectView

from config.worker_health import CHAIN_RUNNER_HEALTH_GROUP
from config.worker_health import WORKER_HEALTH_MAX_AGE_SECONDS
from config.worker_health import worker_health_status
from core.dashboard_metrics import build_dashboard_metrics
from core.monitoring import OperationalRiskService


class HomeView(RedirectView):
    pattern_name = "admin:index"


def worker_health_for_request(request=None):
    # Web 在每次请求内独立判断心跳是否过期，不能依赖可能已停摆的 worker 刷新风险计数。
    # 页面与侧栏共享本次快照，既减少缓存读取，也避免临界时刻同页显示不同状态。
    if request is not None and hasattr(request, "xcash_worker_health"):
        return request.xcash_worker_health
    health = worker_health_status()
    if request is not None:
        request.xcash_worker_health = health
    return health


def _operational_inspection_risk_count(request=None) -> int:
    if request is not None and hasattr(request, "_xcash_operational_risk_count"):
        return request._xcash_operational_risk_count

    # 侧边栏 badge 只需要轻量计数。webhook 堆积量是轻量 DB count，实时取即可；
    # EVM/Tron 资源水位需多链实时 RPC，改读异步巡检写入的缓存，避免每次页面渲染触发链上请求。
    risk_summary = OperationalRiskService.build_summary(limit=0)
    resource_risk_counts = OperationalRiskService.cached_resource_risk_counts()
    risk_count = (
        (0 if settings.ADMIN_PATH_CONFIGURED else 1)
        + risk_summary["stalled_webhook_event_count"]
        + resource_risk_counts["evm_low_native_balance_count"]
        + resource_risk_counts["tron_low_resource_count"]
        + worker_health_for_request(request)["risk_count"]
    )
    if request is not None:
        request._xcash_operational_risk_count = risk_count
    return risk_count


def operational_inspection_sidebar_badge(request):
    return _operational_inspection_risk_count(request)


def has_operational_inspection_risk(request):
    return _operational_inspection_risk_count(request) > 0


def has_no_operational_inspection_risk(request):
    return not has_operational_inspection_risk(request)


def _fmt_usd(amount) -> str:
    return f"$ {amount:,.2f}"


def _fmt_int(value) -> str:
    if value is None:
        return "-"
    return f"{int(value):,}"


def _address_change_href(sender) -> str:
    if sender is None or sender.pk is None:
        return ""
    return reverse("admin:chains_address_change", args=[sender.pk])


# 展示层语义色：unfold 的预编译 CSS 只包含它自己用到的类，项目模板里自造的
# Tailwind 颜色类（bg-emerald-50 之类）不会生效。看板与巡检页统一走
# core/css/admin.css 里定义的 xc-* 语义类，配色跟随 unfold 变量与深色模式。
TONE_ICONS = {
    "danger": "error",
    "warning": "warning",
    "success": "check_circle",
    "info": "info",
    "neutral": "info",
}


def _tone_or_neutral(count: int, *, tone: str) -> str:
    """风险类指标只有真的有风险时才着色，避免整页常态飘红。"""
    return tone if int(count) > 0 else "neutral"


def _metric_card(
    *,
    title,
    metric,
    subtitle,
    tone: str = "neutral",
    icon: str = "insights",
    href: str = "",
) -> dict:
    return {
        "title": title,
        "metric": metric,
        "subtitle": subtitle,
        "tone": tone,
        "icon": icon,
        "href": href,
        "metric_class": "xc-metric-value"
        + (f" xc-value-{tone}" if tone in ("success", "warning", "danger") else ""),
        "card_class": f"xc-metric xc-metric-{tone}",
        "icon_class": (
            f"xc-icon-badge xc-icon-{tone}" if tone != "neutral" else "xc-icon-badge"
        ),
    }


def _inspection_row(
    *,
    level,
    title,
    description,
    href: str = "",
    tone: str,
) -> dict:
    return {
        "level": level,
        "title": title,
        "description": description,
        "href": href,
        "tone": tone,
        "icon": TONE_ICONS.get(tone, "info"),
        "icon_class": f"xc-icon-badge xc-icon-{tone}",
    }


def _inspection_section(
    *,
    title,
    subtitle,
    rows: list[dict],
    empty_text,
    tone: str = "neutral",
) -> dict:
    active_tone = tone if rows else "success"
    return {
        "title": title,
        "subtitle": subtitle,
        "count": len(rows),
        "rows": rows,
        "empty_text": empty_text,
        "tone": active_tone,
        "icon_class": f"xc-icon-badge xc-icon-{active_tone}",
        "icon": "check_circle" if not rows else TONE_ICONS.get(tone, "info"),
    }


def _empty_resource_risk_summary() -> dict:
    return {
        "evm_low_native_balance_count": 0,
        "recent_evm_low_native_balance_alerts": [],
        "tron_low_resource_count": 0,
        "recent_tron_low_resource_alerts": [],
    }


def _build_admin_security_rows() -> list[dict]:
    if settings.ADMIN_PATH_CONFIGURED:
        return []
    return [
        _inspection_row(
            level=_("中"),
            title=_("后台入口未配置"),
            description=_(
                "ADMIN_PATH 未设置，后台仍使用默认入口；建议配置独立后台路径。"
            ),
            href="",
            tone="warning",
        )
    ]


def _build_evm_resource_rows(resource_risk_summary: dict) -> list[dict]:
    rows = []
    for alert in resource_risk_summary["recent_evm_low_native_balance_alerts"]:
        chain = alert.get("chain")
        sender = alert.get("sender")
        error = alert.get("error") or ""
        if error:
            description = _(
                "%(chain)s / %(sender)s / 任务 %(task_count)s 个 / RPC 错误：%(error)s"
            ) % {
                "chain": chain.code if chain else "-",
                "sender": sender.address if sender else "-",
                "task_count": alert.get("task_count") or 0,
                "error": error,
            }
        else:
            description = _(
                "%(chain)s / %(sender)s / 当前 %(current)s wei / 需要 %(required)s wei / 任务 %(task_count)s 个"
            ) % {
                "chain": chain.code if chain else "-",
                "sender": sender.address if sender else "-",
                "current": _fmt_int(alert.get("current_balance")),
                "required": _fmt_int(alert.get("required_balance")),
                "task_count": alert.get("task_count") or 0,
            }
        rows.append(
            _inspection_row(
                level=_("高"),
                title=_("EVM Gas 余额不足"),
                description=description,
                href=_address_change_href(sender),
                tone="danger",
            )
        )
    return rows


def _build_tron_resource_rows(resource_risk_summary: dict) -> list[dict]:
    rows = []
    for alert in resource_risk_summary["recent_tron_low_resource_alerts"]:
        chain = alert.get("chain")
        sender = alert.get("sender")
        error = alert.get("error") or ""
        if error:
            description = _(
                "%(chain)s / %(sender)s / 任务 %(task_count)s 个 / 资源查询错误：%(error)s"
            ) % {
                "chain": chain.code if chain else "-",
                "sender": sender.address if sender else "-",
                "task_count": alert.get("task_count") or 0,
                "error": error,
            }
        else:
            description = _(
                "%(chain)s / %(sender)s / Energy %(energy)s/%(required_energy)s / Bandwidth %(bandwidth)s/%(required_bandwidth)s / 任务 %(task_count)s 个"
            ) % {
                "chain": chain.code if chain else "-",
                "sender": sender.address if sender else "-",
                "energy": _fmt_int(alert.get("available_energy")),
                "required_energy": _fmt_int(alert.get("required_energy")),
                "bandwidth": _fmt_int(alert.get("available_bandwidth")),
                "required_bandwidth": _fmt_int(alert.get("required_bandwidth")),
                "task_count": alert.get("task_count") or 0,
            }
        rows.append(
            _inspection_row(
                level=_("高"),
                title=_("Tron 资源不足"),
                description=description,
                href=_address_change_href(sender),
                tone="danger",
            )
        )
    return rows


def build_worker_health_rows(health):
    if health["status"] == "unhealthy":
        return [
            _inspection_row(
                level=_("高"),
                title=_("无法读取任务消费心跳"),
                description=_(
                    "请检查 Redis 连接与服务日志，当前无法确认任务处理是否正常。"
                ),
                tone="danger",
            )
        ]
    messages = {
        "celery": (
            _("业务任务消费心跳异常"),
            _(
                "尚未收到心跳，或最近 %(seconds)s 秒内没有新鲜的调度与执行回执。"
                "请检查 worker、Beat、Redis 与任务积压。"
            ),
        ),
        CHAIN_RUNNER_HEALTH_GROUP: (
            _("链扫描进程心跳异常"),
            _(
                "最近 %(seconds)s 秒内没有收到 chain-runner 心跳。"
                "请检查 chain-runner 进程、数据库与 Redis。"
            ),
        ),
    }
    return [
        _inspection_row(
            level=_("高"),
            title=messages[group][0],
            description=messages[group][1] % {"seconds": WORKER_HEALTH_MAX_AGE_SECONDS},
            tone="danger",
        )
        for group in health["groups"]
    ]


def _build_operational_inspection_payload(
    metrics, resource_risk_summary=None, *, worker_health=None
):
    # 改动原因：首页摘要与独立巡检页必须共用同一套异常组装逻辑，避免两个入口出现口径漂移。
    inspection_sections = []
    attention_items = []
    resource_risk_summary = resource_risk_summary or _empty_resource_risk_summary()
    worker_health = (
        worker_health if worker_health is not None else worker_health_for_request()
    )

    admin_security_rows = _build_admin_security_rows()
    inspection_sections.append(
        _inspection_section(
            title=_("后台安全配置"),
            subtitle=_("后台入口路径与基础安全配置检查"),
            rows=admin_security_rows,
            empty_text=_("当前没有后台安全配置风险"),
            tone="warning",
        )
    )
    attention_items.extend(admin_security_rows)

    evm_resource_rows = _build_evm_resource_rows(resource_risk_summary)
    inspection_sections.append(
        _inspection_section(
            title=_("EVM Gas 水位巡检"),
            subtitle=_("主动上链任务 sender 原生币余额检查"),
            rows=evm_resource_rows,
            empty_text=_("当前没有 EVM Gas 余额不足的 sender"),
            tone="danger",
        )
    )
    attention_items.extend(evm_resource_rows)

    tron_resource_rows = _build_tron_resource_rows(resource_risk_summary)
    inspection_sections.append(
        _inspection_section(
            title=_("Tron 资源水位巡检"),
            subtitle=_("待广播或需重签任务的 Energy / Bandwidth 检查"),
            rows=tron_resource_rows,
            empty_text=_("当前没有 Tron 资源不足的 sender"),
            tone="danger",
        )
    )
    attention_items.extend(tron_resource_rows)

    worker_rows = build_worker_health_rows(worker_health)
    inspection_sections.append(
        _inspection_section(
            title=_("任务消费巡检"),
            subtitle=_("业务任务与链扫描的执行心跳"),
            rows=worker_rows,
            empty_text=_("业务任务与链扫描的消费心跳均正常"),
            tone="danger",
        )
    )
    attention_items.extend(worker_rows)

    failed_attempt_rows = [
        _inspection_row(
            level=_("高"),
            title=_("Webhook 投递失败"),
            description=_("项目 %(project)s 在 %(time)s 投递失败：HTTP %(status)s")
            % {
                "project": attempt.event.project.name,
                "time": attempt.created_at.strftime("%m-%d %H:%M"),
                "status": attempt.response_status or "-",
            },
            href=reverse("admin:webhooks_deliveryattempt_change", args=[attempt.pk]),
            tone="danger",
        )
        for attempt in metrics["recent_failed_attempts"]
    ]
    inspection_sections.append(
        _inspection_section(
            title=_("Webhook 投递失败"),
            subtitle=_("近24小时失败回调明细"),
            rows=failed_attempt_rows,
            empty_text=_("近24小时没有新的投递失败"),
            tone="danger",
        )
    )
    attention_items.extend(failed_attempt_rows)

    stalled_invoice_rows = [
        _inspection_row(
            level=_("中"),
            title=_("账单收款长时间待链上确认"),
            description=_("%(project)s / %(sys_no)s / %(crypto)s-%(chain)s")
            % {
                "project": invoice.project.name,
                "sys_no": invoice.sys_no,
                "crypto": invoice.crypto.symbol if invoice.crypto else "-",
                "chain": invoice.chain.code if invoice.chain else "-",
            },
            href=reverse("admin:invoices_invoice_change", args=[invoice.pk]),
            tone="warning",
        )
        for invoice in metrics["recent_stalled_invoices"]
    ]
    inspection_sections.append(
        _inspection_section(
            title=_("链上确认巡检"),
            subtitle=_("已观察到付款但长时间未满足确认数的账单收款"),
            rows=stalled_invoice_rows,
            empty_text=_("当前没有长时间待链上确认的账单收款"),
            tone="warning",
        )
    )
    attention_items.extend(stalled_invoice_rows)

    stalled_webhook_rows = [
        _inspection_row(
            level=_("高"),
            title=_("Webhook 长时间未送达"),
            description=_("%(project)s / %(nonce)s / 创建于 %(time)s")
            % {
                "project": event.project.name,
                "nonce": event.nonce,
                "time": event.created_at.strftime("%m-%d %H:%M"),
            },
            href=reverse("admin:webhooks_webhookevent_change", args=[event.pk]),
            tone="danger",
        )
        for event in metrics["recent_stalled_webhook_events"]
    ]
    inspection_sections.append(
        _inspection_section(
            title=_("Webhook 堆积巡检"),
            subtitle=_("创建后长时间未送达的事件"),
            rows=stalled_webhook_rows,
            empty_text=_("当前没有堆积中的 Webhook 事件"),
            tone="danger",
        )
    )
    attention_items.extend(stalled_webhook_rows)

    return {
        "attention_items": attention_items,
        "inspection_sections": inspection_sections,
    }


def _build_operational_inspection_summary_cards(
    snapshot, resource_risk_summary, worker_health
):
    # 改动原因：独立巡检页需要先给出风险摘要，用户不必逐段滚动才能判断当前是否有异常。
    admin_path_configured = settings.ADMIN_PATH_CONFIGURED
    admin_path_risk = 0 if admin_path_configured else 1
    evm_gas_risk = resource_risk_summary["evm_low_native_balance_count"]
    tron_resource_risk = resource_risk_summary["tron_low_resource_count"]
    return [
        _metric_card(
            title=_("任务消费"),
            metric=worker_health["risk_count"],
            subtitle=(
                _("两组消费心跳均正常")
                if worker_health["status"] == "ok"
                else _("消费心跳异常，请查看巡检明细")
            ),
            tone=_tone_or_neutral(worker_health["risk_count"], tone="danger"),
            icon="monitor_heart",
        ),
        _metric_card(
            title=_("后台安全"),
            metric=admin_path_risk,
            subtitle=(
                _("ADMIN_PATH 已配置")
                if admin_path_configured
                else _("ADMIN_PATH 未设置")
            ),
            tone=_tone_or_neutral(admin_path_risk, tone="warning"),
            icon="lock",
        ),
        _metric_card(
            title=_("EVM Gas"),
            metric=evm_gas_risk,
            subtitle=_("Gas 余额不足 sender %(count)s 个") % {"count": evm_gas_risk},
            tone=_tone_or_neutral(evm_gas_risk, tone="danger"),
            icon="local_gas_station",
        ),
        _metric_card(
            title=_("Tron 资源"),
            metric=tron_resource_risk,
            subtitle=_("Energy / Bandwidth 不足 sender %(count)s 个")
            % {"count": tron_resource_risk},
            tone=_tone_or_neutral(tron_resource_risk, tone="danger"),
            icon="bolt",
        ),
        _metric_card(
            title=_("链上确认"),
            metric=snapshot["confirming_count"],
            subtitle=_("待链上确认 %(count)s 笔，临近超时 %(soon)s 笔")
            % {
                "count": snapshot["confirming_count"],
                "soon": snapshot["expiring_soon_count"],
            },
            tone=_tone_or_neutral(snapshot["confirming_count"], tone="warning"),
            icon="hourglass_top",
        ),
        _metric_card(
            title=_("Webhook 堆积"),
            metric=snapshot["stalled_webhook_event_count"],
            subtitle=_("待投递 %(pending)s 条，失败事件 %(failed)s 条")
            % {
                "pending": snapshot["pending_events_count"],
                "failed": snapshot["failed_events_count"],
            },
            tone=_tone_or_neutral(
                snapshot["stalled_webhook_event_count"], tone="danger"
            ),
            icon="webhook",
        ),
    ]


# 30 日趋势图配色跟随 unfold 的 CSS 变量：app.js 会在渲染时把 var(--color-x)
# 解析成实际色值，因此切换主题 / 改 UNFOLD["COLORS"] 时图表自动同步。
CHART_MONEY_COLOR = "var(--color-primary-500)"
CHART_CREATED_COLOR = "var(--color-blue-300)"
CHART_EXPIRED_COLOR = "var(--color-orange-300)"


def _build_trend_chart(chart_rows) -> str:
    """把 30 日趋势拼成 Chart.js 数据结构。

    金额与笔数量级差两三个数量级，必须分左右两轴；同时显式打开图例，
    否则 unfold 默认隐藏图例，三条序列在图上无法区分。
    """
    return json.dumps(
        {
            "labels": [row["label"] for row in chart_rows],
            "datasets": [
                {
                    "label": str(_("成交金额 (USD)")),
                    "type": "line",
                    "yAxisID": "y",
                    "order": 0,
                    "data": [float(row["completed_worth"]) for row in chart_rows],
                    "backgroundColor": CHART_MONEY_COLOR,
                    "borderColor": CHART_MONEY_COLOR,
                    "borderWidth": 2,
                    "tension": 0.35,
                },
                {
                    "label": str(_("创建账单")),
                    "type": "bar",
                    "yAxisID": "y1",
                    "order": 1,
                    "data": [row["created_count"] for row in chart_rows],
                    "backgroundColor": CHART_CREATED_COLOR,
                    "borderColor": CHART_CREATED_COLOR,
                },
                {
                    "label": str(_("超时账单")),
                    "type": "bar",
                    "yAxisID": "y1",
                    "order": 2,
                    "data": [row["expired_count"] for row in chart_rows],
                    "backgroundColor": CHART_EXPIRED_COLOR,
                    "borderColor": CHART_EXPIRED_COLOR,
                },
            ],
        },
    )


def _build_trend_chart_options() -> str:
    """趋势图的 Chart.js options。

    注意 unfold 的 app.js 在传入 data-options 时【整体替换】默认配置，
    所以 scales.x / scales.y 的 grid 必须显式声明：深色模式切换时
    changeDarkModeSettings() 正是通过这两个对象回写网格线颜色。
    """
    axis_ticks = {"color": "#9ca3af"}
    return json.dumps(
        {
            "responsive": True,
            "maintainAspectRatio": False,
            "interaction": {"mode": "index", "intersect": False},
            "plugins": {
                "legend": {
                    "display": True,
                    "position": "top",
                    "align": "end",
                    "labels": {
                        "boxHeight": 6,
                        "boxWidth": 6,
                        "color": "#9ca3af",
                        "pointStyle": "circle",
                        "usePointStyle": True,
                    },
                },
                "tooltip": {"enabled": True},
            },
            "scales": {
                "x": {
                    "grid": {"display": False, "tickWidth": 0},
                    "border": {"width": 0},
                    "ticks": {**axis_ticks, "maxTicksLimit": 10},
                },
                "y": {
                    "position": "left",
                    "beginAtZero": True,
                    "grid": {"tickWidth": 0},
                    "border": {"dash": [5, 5], "width": 0},
                    "ticks": axis_ticks,
                },
                "y1": {
                    "position": "right",
                    "beginAtZero": True,
                    "grid": {"display": False, "tickWidth": 0},
                    "border": {"width": 0},
                    "ticks": {**axis_ticks, "precision": 0},
                },
            },
        },
    )


def dashboard_callback(request, context):
    # analytics app 已退役，首页实时指标改由 core 内部服务直接提供。
    metrics = build_dashboard_metrics()
    snapshot = metrics["snapshot"]
    chart_rows = metrics["chart_rows"]
    inspection_payload = _build_operational_inspection_payload(
        metrics, worker_health=worker_health_for_request(request)
    )
    invoice_changelist = reverse("admin:invoices_invoice_changelist")
    event_changelist = reverse("admin:webhooks_webhookevent_changelist")

    # 第一排只放「赚了多少钱」：今日 / 7 日 / 30 日成交额，是打开后台第一眼要看的结论。
    revenue_cards = [
        _metric_card(
            title=_("今日成交额"),
            metric=_fmt_usd(snapshot["today_completed_worth"]),
            subtitle=_("成功账单 %(count)s 笔")
            % {"count": snapshot["today_completed_count"]},
            tone="success",
            icon="today",
        ),
        _metric_card(
            title=_("近 7 日成交额"),
            metric=_fmt_usd(snapshot["rolling_7d_completed_worth"]),
            subtitle=_("成功账单 %(count)s 笔")
            % {"count": snapshot["rolling_7d_completed_count"]},
            tone="info",
            icon="date_range",
        ),
        _metric_card(
            title=_("近 30 日成交额"),
            metric=_fmt_usd(snapshot["rolling_30d_completed_worth"]),
            subtitle=_("成功账单 %(count)s 笔")
            % {"count": snapshot["rolling_30d_completed_count"]},
            tone="primary",
            icon="calendar_month",
        ),
    ]

    # 第二排是「健康度」：转化、在途资金、回调成功率。带比率的两项额外给进度条，
    # 让百分比不用读数字就能感知高低。
    health_cards = [
        {
            **_metric_card(
                title=_("30 日转化率"),
                metric=f"{snapshot['conversion_rate_30d']}%",
                subtitle=_("近 30 日共创建 %(count)s 笔账单")
                % {"count": snapshot["created_30d_count"]},
                tone="neutral",
                icon="conversion_path",
            ),
            "progress": float(snapshot["conversion_rate_30d"]),
        },
        _metric_card(
            title=_("待链上确认"),
            metric=_fmt_usd(snapshot["confirming_worth"]),
            subtitle=_("已观察到付款 %(count)s 笔")
            % {"count": snapshot["confirming_count"]},
            tone=_tone_or_neutral(snapshot["confirming_count"], tone="warning"),
            icon="hourglass_top",
            href=f"{invoice_changelist}?status__exact=waiting&transfer__isnull=False",
        ),
        {
            **_metric_card(
                title=_("Webhook 成功率"),
                metric=f"{snapshot['webhook_success_rate_7d']}%",
                subtitle=_("近 7 日投递 %(total)s 次，失败 %(failed)s 次")
                % {
                    "total": snapshot["webhook_attempt_total_7d"],
                    "failed": snapshot["webhook_attempt_failed_7d"],
                },
                tone=_tone_or_neutral(
                    snapshot["webhook_attempt_failed_7d"], tone="danger"
                ),
                icon="webhook",
            ),
            "progress": float(snapshot["webhook_success_rate_7d"]),
        },
    ]

    backlog_rows = [
        {
            "label": _("待支付"),
            "value": snapshot["waiting_count"],
            "detail": _fmt_usd(snapshot["waiting_worth"]),
            "icon": "schedule",
            "tone": "neutral",
            "href": f"{invoice_changelist}?status__exact=waiting",
        },
        {
            "label": _("待链上确认"),
            "value": snapshot["confirming_count"],
            "detail": _fmt_usd(snapshot["confirming_worth"]),
            "icon": "hourglass_top",
            "tone": _tone_or_neutral(snapshot["confirming_count"], tone="warning"),
            "href": f"{invoice_changelist}?status__exact=waiting&transfer__isnull=False",
        },
        {
            "label": _("待投递事件"),
            "value": snapshot["pending_events_count"],
            "detail": _("等待 Webhook 调度"),
            "icon": "outbox",
            "tone": "neutral",
            "href": f"{event_changelist}?status__exact=pending",
        },
        {
            "label": _("失败事件"),
            "value": snapshot["failed_events_count"],
            "detail": _("需要人工检查或重投"),
            "icon": "error",
            "tone": _tone_or_neutral(snapshot["failed_events_count"], tone="danger"),
            "href": f"{event_changelist}?status__exact=failed",
        },
    ]

    for row in backlog_rows:
        row["icon_class"] = f"xc-icon-badge xc-icon-{row['tone']}"

    context.update(
        {
            "revenue_cards": revenue_cards,
            "health_cards": health_cards,
            "backlog_rows": backlog_rows,
            "top_projects": [
                {
                    "name": row["project__name"],
                    "gmv": _fmt_usd(row["gmv"]),
                    "completed_orders": row["completed_orders"],
                    "conversion_rate": (
                        f"{(row['conversion_completed_orders'] / row['total_orders'] * 100):.1f}%"
                        if row["total_orders"]
                        else "0.0%"
                    ),
                    "waiting_orders": row["waiting_orders"],
                    "confirming_orders": row["confirming_orders"],
                }
                for row in metrics["top_projects"]
            ],
            "payment_methods": _build_payment_method_rows(metrics["payment_methods"]),
            "attention_items": inspection_payload["attention_items"][:6],
            "attention_total": len(inspection_payload["attention_items"]),
            "inspection_url": reverse("operational-inspection"),
            "chart": _build_trend_chart(chart_rows),
            "chart_options": _build_trend_chart_options(),
        },
    )
    return context


def _build_payment_method_rows(payment_methods) -> list[dict]:
    """收款方式分布补上占比，让「哪条链在扛量」一眼可见。"""
    total_gmv = sum(row["gmv"] for row in payment_methods)
    rows = []
    for row in payment_methods:
        share = float(row["gmv"] / total_gmv * 100) if total_gmv else 0.0
        rows.append(
            {
                "symbol": row["crypto__symbol"],
                "chain": row["chain__code"],
                "gmv": _fmt_usd(row["gmv"]),
                # 计数单位必须走 gettext：写在模板里拼 "笔" 会在英文界面露出中文。
                "order_label": _("%(count)s 笔") % {"count": row["order_count"]},
                "share": round(share, 1),
            }
        )
    return rows


def operational_inspection_view(request):
    # 改动原因：“异常巡检”菜单需要落到独立页面，而不是继续复用 admin 首页。
    metrics = build_dashboard_metrics()
    worker_health = worker_health_for_request(request)
    resource_risk_summary = OperationalRiskService.build_summary(
        limit=4,
        include_resource_checks=True,
    )
    OperationalRiskService.cache_resource_risk_counts(
        evm_low_native_balance_count=resource_risk_summary[
            "evm_low_native_balance_count"
        ],
        tron_low_resource_count=resource_risk_summary["tron_low_resource_count"],
    )
    inspection_payload = _build_operational_inspection_payload(
        metrics,
        resource_risk_summary=resource_risk_summary,
        worker_health=worker_health,
    )
    overview_context = admin.site.each_context(request)
    overview_context.update(
        {
            "title": _("异常巡检"),
            "inspection_summary_cards": _build_operational_inspection_summary_cards(
                metrics["snapshot"],
                resource_risk_summary,
                worker_health,
            ),
            "inspection_sections": inspection_payload["inspection_sections"],
            "attention_items_count": len(inspection_payload["attention_items"]),
        }
    )
    return render(request, "admin/operational_inspection.html", overview_context)
