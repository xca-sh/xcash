import json

from django.contrib import admin
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import RedirectView

from core.dashboard_metrics import build_dashboard_metrics
from core.monitoring import OperationalRiskService


class HomeView(RedirectView):
    pattern_name = "admin:index"


def _build_environment_badge(risk_summary: dict) -> list[str]:
    """为后台顶部角标生成轻量状态摘要，避免复用完整首页聚合。"""
    pending_count = (
        risk_summary["stalled_webhook_event_count"]
        + risk_summary["evm_low_native_balance_count"]
        + risk_summary["tron_low_resource_count"]
    )
    if pending_count > 0:
        return [_("存在高风险告警"), "danger"]

    return [_("运行正常"), "success"]


def environment_callback(request):
    # 顶部 environment badge 只需要判断是否有高风险积压，
    # 这里仅读取巡检计数，避免触发完整首页统计查询。
    risk_summary = OperationalRiskService.build_summary(limit=0)
    return _build_environment_badge(risk_summary)


def _fmt_usd(amount) -> str:
    return f"$ {amount:,.2f}"


def _build_operational_inspection_payload(metrics):
    # 改动原因：首页摘要与独立巡检页必须共用同一套异常组装逻辑，避免两个入口出现口径漂移。
    inspection_sections = []
    attention_items = []

    failed_attempt_rows = [
        {
            "level": _("高"),
            "title": _("Webhook 投递失败"),
            "description": _("项目 %(project)s 在 %(time)s 投递失败：HTTP %(status)s")
            % {
                "project": attempt.event.project.name,
                "time": attempt.created_at.strftime("%m-%d %H:%M"),
                "status": attempt.response_status or "-",
            },
            "href": reverse("admin:webhooks_deliveryattempt_change", args=[attempt.pk]),
        }
        for attempt in metrics["recent_failed_attempts"]
    ]
    inspection_sections.append(
        {
            "title": _("Webhook 投递失败"),
            "subtitle": _("近24小时失败回调明细"),
            "count": len(failed_attempt_rows),
            "rows": failed_attempt_rows,
            "empty_text": _("近24小时没有新的投递失败"),
        }
    )
    attention_items.extend(failed_attempt_rows)

    stalled_invoice_rows = [
        {
            "level": _("中"),
            "title": _("账单收款长时间待链上确认"),
            "description": _("%(project)s / %(sys_no)s / %(crypto)s-%(chain)s")
            % {
                "project": invoice.project.name,
                "sys_no": invoice.sys_no,
                "crypto": invoice.crypto.symbol if invoice.crypto else "-",
                "chain": invoice.chain.code if invoice.chain else "-",
            },
            "href": reverse("admin:invoices_invoice_change", args=[invoice.pk]),
        }
        for invoice in metrics["recent_stalled_invoices"]
    ]
    inspection_sections.append(
        {
            "title": _("链上确认巡检"),
            "subtitle": _("已观察到付款但长时间未满足确认数的账单收款"),
            "count": len(stalled_invoice_rows),
            "rows": stalled_invoice_rows,
            "empty_text": _("当前没有长时间待链上确认的账单收款"),
        }
    )
    attention_items.extend(stalled_invoice_rows)

    stalled_webhook_rows = [
        {
            "level": _("高"),
            "title": _("Webhook 长时间未送达"),
            "description": _("%(project)s / %(nonce)s / 创建于 %(time)s")
            % {
                "project": event.project.name,
                "nonce": event.nonce,
                "time": event.created_at.strftime("%m-%d %H:%M"),
            },
            "href": reverse("admin:webhooks_webhookevent_change", args=[event.pk]),
        }
        for event in metrics["recent_stalled_webhook_events"]
    ]
    inspection_sections.append(
        {
            "title": _("Webhook 堆积巡检"),
            "subtitle": _("创建后长时间未送达的事件"),
            "count": len(stalled_webhook_rows),
            "rows": stalled_webhook_rows,
            "empty_text": _("当前没有堆积中的 Webhook 事件"),
        }
    )
    attention_items.extend(stalled_webhook_rows)

    return {
        "attention_items": attention_items,
        "inspection_sections": inspection_sections,
    }


def _build_operational_inspection_summary_cards(snapshot):
    # 改动原因：独立巡检页需要先给出风险摘要，用户不必逐段滚动才能判断当前是否有异常。
    return [
        {
            "title": _("链上确认风险"),
            "metric": snapshot["confirming_count"],
            "subtitle": _("待链上确认 %(count)s 笔，临近超时 %(soon)s 笔")
            % {
                "count": snapshot["confirming_count"],
                "soon": snapshot["expiring_soon_count"],
            },
            "tone": "bg-amber-50",
        },
        {
            "title": _("Webhook 巡检"),
            "metric": snapshot["stalled_webhook_event_count"],
            "subtitle": _("待投递 %(pending)s 条，失败事件 %(failed)s 条")
            % {
                "pending": snapshot["pending_events_count"],
                "failed": snapshot["failed_events_count"],
            },
            "tone": "bg-sky-50",
        },
    ]


def dashboard_callback(request, context):
    # analytics app 已退役，首页实时指标改由 core 内部服务直接提供。
    metrics = build_dashboard_metrics()
    snapshot = metrics["snapshot"]
    chart_rows = metrics["chart_rows"]
    inspection_payload = _build_operational_inspection_payload(metrics)

    # 后台首页改为实时经营看板，优先展示商户最关心的成交、转化、积压和失败指标。
    snapshot_cards = [
        {
            "title": _("今日成交额"),
            "metric": _fmt_usd(snapshot["today_completed_worth"]),
            "subtitle": _("今日成功账单收款 %(count)s 笔")
            % {"count": snapshot["today_completed_count"]},
            "tone": "bg-emerald-50",
        },
        {
            "title": _("7日成交额"),
            "metric": _fmt_usd(snapshot["rolling_7d_completed_worth"]),
            "subtitle": _("近7日成功账单收款 %(count)s 笔")
            % {"count": snapshot["rolling_7d_completed_count"]},
            "tone": "bg-sky-50",
        },
        {
            "title": _("30日成交额"),
            "metric": _fmt_usd(snapshot["rolling_30d_completed_worth"]),
            "subtitle": _("近30日成功账单收款 %(count)s 笔")
            % {"count": snapshot["rolling_30d_completed_count"]},
            "tone": "bg-indigo-50",
        },
        {
            "title": _("30日账单收款转化率"),
            "metric": f"{snapshot['conversion_rate_30d']}%",
            "subtitle": _("近30日共创建账单收款 %(count)s 笔")
            % {"count": snapshot["created_30d_count"]},
            "tone": "bg-amber-50",
        },
        {
            "title": _("待链上确认收款"),
            "metric": _fmt_usd(snapshot["confirming_worth"]),
            "subtitle": _("已观察到付款 %(count)s 笔")
            % {"count": snapshot["confirming_count"]},
            "tone": "bg-orange-50",
        },
        {
            "title": _("Webhook 健康度"),
            "metric": f"{snapshot['webhook_success_rate_7d']}%",
            "subtitle": _("近7日投递 %(total)s 次，失败投递 %(failed)s 次")
            % {
                "total": snapshot["webhook_attempt_total_7d"],
                "failed": snapshot["webhook_attempt_failed_7d"],
            },
            "tone": "bg-rose-50",
        },
    ]

    backlog_rows = [
        {
            "label": _("待支付"),
            "value": snapshot["waiting_count"],
            "detail": _fmt_usd(snapshot["waiting_worth"]),
            "href": f"{reverse('admin:invoices_invoice_changelist')}?status__exact=waiting",
        },
        {
            "label": _("待链上确认账单收款"),
            "value": snapshot["confirming_count"],
            "detail": _fmt_usd(snapshot["confirming_worth"]),
            "href": f"{reverse('admin:invoices_invoice_changelist')}?status__exact=waiting&transfer__isnull=False",
        },
        {
            "label": _("待投递事件"),
            "value": snapshot["pending_events_count"],
            "detail": _("等待 Webhook 调度"),
            "href": f"{reverse('admin:webhooks_webhookevent_changelist')}?status__exact=pending",
        },
        {
            "label": _("失败事件"),
            "value": snapshot["failed_events_count"],
            "detail": _("需要人工检查或重投"),
            "href": f"{reverse('admin:webhooks_webhookevent_changelist')}?status__exact=failed",
        },
    ]

    health_cards = [
        {
            "title": _("Webhook 投递"),
            "metric": _("%(ok)s / %(total)s 成功")
            % {
                "ok": snapshot["webhook_attempt_ok_7d"],
                "total": snapshot["webhook_attempt_total_7d"],
            },
            "subtitle": _("近7日成功率 %(rate)s%%")
            % {"rate": snapshot["webhook_success_rate_7d"]},
        },
        {
            "title": _("任务巡检"),
            "metric": snapshot["stalled_webhook_event_count"],
            "subtitle": _("超时回调"),
        },
    ]

    context.update(
        {
            "snapshot_cards": snapshot_cards,
            "backlog_rows": backlog_rows,
            "health_cards": health_cards,
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
            "payment_methods": [
                {
                    "label": f"{row['crypto__symbol']} / {row['chain__code']}",
                    "gmv": _fmt_usd(row["gmv"]),
                    "order_count": row["order_count"],
                }
                for row in metrics["payment_methods"]
            ],
            "attention_items": inspection_payload["attention_items"][:8],
            "chart": json.dumps(
                {
                    "labels": [row["label"] for row in chart_rows],
                    "datasets": [
                        {
                            "label": str(_("完成金额(USD)")),
                            "type": "line",
                            "yAxisID": "y",
                            "data": [
                                float(row["completed_worth"]) for row in chart_rows
                            ],
                            "backgroundColor": "#0f766e",
                            "borderColor": "#0f766e",
                            "tension": 0.35,
                        },
                        {
                            "label": str(_("创建账单收款数")),
                            "type": "bar",
                            "yAxisID": "y1",
                            "data": [row["created_count"] for row in chart_rows],
                            "backgroundColor": "#93c5fd",
                            "borderColor": "#60a5fa",
                        },
                        {
                            "label": str(_("超时账单收款数")),
                            "type": "bar",
                            "yAxisID": "y1",
                            "data": [row["expired_count"] for row in chart_rows],
                            "backgroundColor": "#fdba74",
                            "borderColor": "#fb923c",
                        },
                    ],
                },
            ),
        },
    )
    return context


def operational_inspection_view(request):
    # 改动原因：“异常巡检”菜单需要落到独立页面，而不是继续复用 admin 首页。
    metrics = build_dashboard_metrics()
    inspection_payload = _build_operational_inspection_payload(metrics)
    overview_context = admin.site.each_context(request)
    overview_context.update(
        {
            "title": _("异常巡检"),
            "inspection_summary_cards": _build_operational_inspection_summary_cards(
                metrics["snapshot"],
            ),
            "inspection_sections": inspection_payload["inspection_sections"],
            "attention_items_count": len(inspection_payload["attention_items"]),
        }
    )
    return render(request, "admin/operational_inspection.html", overview_context)
