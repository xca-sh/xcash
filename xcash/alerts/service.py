from __future__ import annotations

from datetime import timedelta

import httpx
from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from alerts.models import ProjectAlertEventType
from alerts.models import ProjectAlertSeverity
from alerts.models import ProjectAlertState
from alerts.models import ProjectAlertStatus
from alerts.models import ProjectTelegramAlertConfig
from core.monitoring import OperationalRiskService
from core.runtime_settings import get_alerts_repeat_interval_minutes
from withdrawals.models import WithdrawalStatus


class TelegramAlertError(Exception):
    pass


class TelegramAlertService:
    def __init__(self):
        self._bot_token = settings.ALERTS_TELEGRAM_BOT_TOKEN.strip()
        self._api_base = settings.ALERTS_TELEGRAM_API_BASE.rstrip("/")
        self._timeout = settings.ALERTS_TELEGRAM_TIMEOUT
        # 平台参数中心允许超管在运行期调整重复告警节流窗口，避免只能靠发版改 settings。
        self._repeat_interval = timedelta(minutes=get_alerts_repeat_interval_minutes())

    @property
    def is_configured(self) -> bool:
        return bool(self._bot_token)

    def send_test_message(self, *, config_id: int) -> None:
        config = ProjectTelegramAlertConfig.objects.select_related("project").get(
            pk=config_id
        )
        text = "\n".join(
            [
                str(_("[TEST] 项目 Telegram 告警测试")),
                str(_("项目: %(project)s") % {"project": config.project.name}),
                str(_("目标: %(target)s") % {"target": config.target_label}),
                str(_("说明: 这条消息用于验证 Telegram 告警配置是否可用。")),
            ]
        )
        self._send_to_telegram(config=config, text=text)
        now = timezone.now()
        config.last_test_sent_at = now
        config.last_verified_at = now
        config.last_error_message = ""
        config.last_error_at = None
        config.save(
            update_fields=(
                "last_test_sent_at",
                "last_verified_at",
                "last_error_message",
                "last_error_at",
            )
        )

    def sync_operational_alerts(self) -> None:
        now = timezone.now()
        active_fingerprints: set[str] = set()
        # 预加载所有启用的项目 Telegram 配置，避免每个 state 都单独查询。
        config_cache: dict[int, ProjectTelegramAlertConfig] = {
            c.project_id: c
            for c in ProjectTelegramAlertConfig.objects.filter(
                enabled=True
            ).select_related("project")
        }

        for withdrawal in OperationalRiskService.stalled_withdrawals():
            state = self._upsert_state(
                project=withdrawal.project,
                event_type=ProjectAlertEventType.WITHDRAWAL_STALLED,
                object_type="withdrawal",
                object_pk=withdrawal.pk,
                severity=(
                    ProjectAlertSeverity.CRITICAL
                    if withdrawal.status != WithdrawalStatus.REVIEWING
                    else ProjectAlertSeverity.HIGH
                ),
                title=str(_("提币长时间未完成")),
                detail=str(
                    _("%(out_no)s / %(status)s / %(crypto)s-%(chain)s")
                    % {
                        "out_no": withdrawal.out_no,
                        "status": withdrawal.get_status_display(),
                        "crypto": withdrawal.crypto.symbol,
                        "chain": withdrawal.chain.code if withdrawal.chain else "-",
                    }
                ),
                admin_url=reverse(
                    "admin:withdrawals_withdrawal_change", args=[withdrawal.pk]
                ),
                seen_at=now,
            )
            active_fingerprints.add(state.fingerprint)
            self._notify_if_due(
                state=state, mode="open", now=now, config_cache=config_cache
            )

        for deposit in OperationalRiskService.stalled_deposit_collections():
            if deposit.customer_id is None:
                continue
            state = self._upsert_state(
                project=deposit.customer.project,
                event_type=ProjectAlertEventType.DEPOSIT_COLLECTION_STALLED,
                object_type="deposit",
                object_pk=deposit.pk,
                severity=ProjectAlertSeverity.HIGH,
                title=str(_("归集长时间未完成")),
                detail=str(
                    _("%(sys_no)s / %(crypto)s-%(chain)s")
                    % {
                        "sys_no": deposit.sys_no,
                        "crypto": deposit.transfer.crypto.symbol,
                        "chain": deposit.transfer.chain.code,
                    }
                ),
                admin_url=reverse("admin:deposits_deposit_change", args=[deposit.pk]),
                seen_at=now,
            )
            active_fingerprints.add(state.fingerprint)
            self._notify_if_due(
                state=state, mode="open", now=now, config_cache=config_cache
            )

        for invoice in OperationalRiskService.stalled_contract_collections():
            state = self._upsert_state(
                project=invoice.project,
                event_type=ProjectAlertEventType.CONTRACT_COLLECTION_STALLED,
                object_type="invoice",
                object_pk=invoice.pk,
                severity=ProjectAlertSeverity.HIGH,
                title=str(_("合约归集长时间未完成")),
                detail=str(
                    _("%(sys_no)s / %(amount)s")
                    % {"sys_no": invoice.sys_no, "amount": str(invoice.amount)}
                ),
                admin_url=reverse(
                    "admin:invoices_invoice_change",
                    args=[invoice.pk],
                ),
                seen_at=now,
            )
            active_fingerprints.add(state.fingerprint)
            self._notify_if_due(
                state=state, mode="open", now=now, config_cache=config_cache
            )

        for event in OperationalRiskService.stalled_webhook_events():
            state = self._upsert_state(
                project=event.project,
                event_type=ProjectAlertEventType.WEBHOOK_STALLED,
                object_type="webhook_event",
                object_pk=event.pk,
                severity=ProjectAlertSeverity.CRITICAL,
                title=str(_("Webhook 长时间未送达")),
                detail=str(_("%(nonce)s / 待投递超时") % {"nonce": event.nonce}),
                admin_url=reverse(
                    "admin:webhooks_webhookevent_change", args=[event.pk]
                ),
                seen_at=now,
            )
            active_fingerprints.add(state.fingerprint)
            self._notify_if_due(
                state=state, mode="open", now=now, config_cache=config_cache
            )

        self._resolve_missing_states(
            active_fingerprints=active_fingerprints,
            resolved_at=now,
            config_cache=config_cache,
        )

    def send_state_message(self, *, state_id: int, mode: str) -> None:
        state = ProjectAlertState.objects.select_related("project").get(pk=state_id)
        config = self._get_project_config(project_id=state.project_id)
        if config is None:
            return
        if mode == "resolved" and not config.notify_on_recovery:
            return
        if mode == "open" and not config.supports_event(state.event_type):
            return

        text = self._format_state_message(state=state, mode=mode)
        try:
            self._send_to_telegram(config=config, text=text)
        except TelegramAlertError as exc:
            state.last_error_message = str(exc)
            state.last_error_at = timezone.now()
            state.save(update_fields=("last_error_message", "last_error_at"))
            config.last_error_message = str(exc)
            config.last_error_at = state.last_error_at
            config.save(update_fields=("last_error_message", "last_error_at"))
            # 重新抛出以便 Celery 任务层进行自动重试
            raise

        now = timezone.now()
        # notify_count 使用 F() 原子递增，避免并发丢失计数
        ProjectAlertState.objects.filter(pk=state.pk).update(
            last_sent_at=now,
            notify_count=models.F("notify_count") + 1,
            last_error_message="",
            last_error_at=None,
        )
        config.last_error_message = ""
        config.last_error_at = None
        config.save(update_fields=("last_error_message", "last_error_at"))

    def _get_project_config(
        self, *, project_id: int
    ) -> ProjectTelegramAlertConfig | None:
        try:
            return ProjectTelegramAlertConfig.objects.select_related("project").get(
                project_id=project_id,
                enabled=True,
            )
        except ProjectTelegramAlertConfig.DoesNotExist:
            return None

    def _upsert_state(
        self,
        *,
        project,
        event_type: str,
        object_type: str,
        object_pk: int,
        severity: str,
        title: str,
        detail: str,
        admin_url: str,
        seen_at,
    ) -> ProjectAlertState:
        fingerprint = f"{project.pk}:{event_type}:{object_type}:{object_pk}"
        state, created = ProjectAlertState.objects.get_or_create(
            fingerprint=fingerprint,
            defaults={
                "project": project,
                "event_type": event_type,
                "object_type": object_type,
                "object_pk": object_pk,
                "severity": severity,
                "title": title,
                "detail": detail,
                "admin_url": admin_url,
                "first_seen_at": seen_at,
                "last_seen_at": seen_at,
            },
        )
        if created:
            return state

        state.project = project
        state.severity = severity
        state.title = title
        state.detail = detail
        state.admin_url = admin_url
        state.last_seen_at = seen_at
        if state.status == ProjectAlertStatus.RESOLVED:
            state.status = ProjectAlertStatus.OPEN
            state.resolved_at = None
        state.save(
            update_fields=(
                "project",
                "severity",
                "title",
                "detail",
                "admin_url",
                "last_seen_at",
                "status",
                "resolved_at",
            )
        )
        return state

    def _notify_if_due(
        self,
        *,
        state: ProjectAlertState,
        mode: str,
        now,
        config_cache: dict[int, ProjectTelegramAlertConfig] | None = None,
    ) -> None:
        if config_cache is not None:
            config = config_cache.get(state.project_id)
        else:
            config = self._get_project_config(project_id=state.project_id)
        if config is None or not config.supports_event(state.event_type):
            return
        if state.last_sent_at and now - state.last_sent_at < self._repeat_interval:
            return

        from alerts.tasks import send_project_telegram_alert

        send_project_telegram_alert.delay(state_id=state.pk, mode=mode)

    def _resolve_missing_states(
        self,
        *,
        active_fingerprints: set[str],
        resolved_at,
        config_cache: dict[int, ProjectTelegramAlertConfig] | None = None,
    ) -> None:
        stale_states = ProjectAlertState.objects.filter(
            event_type__in=ProjectAlertEventType.values,
            status=ProjectAlertStatus.OPEN,
        ).exclude(fingerprint__in=active_fingerprints)

        from alerts.tasks import send_project_telegram_alert

        for state in stale_states:
            state.mark_resolved(resolved_at=resolved_at)
            state.save(update_fields=("status", "resolved_at"))
            if config_cache is not None:
                config = config_cache.get(state.project_id)
            else:
                config = self._get_project_config(project_id=state.project_id)
            if (
                config is None
                or not config.notify_on_recovery
                or not config.supports_event(state.event_type)
            ):
                continue
            send_project_telegram_alert.delay(state_id=state.pk, mode="resolved")

    def _format_state_message(self, *, state: ProjectAlertState, mode: str) -> str:
        if mode == "resolved":
            header = str(_("[RESOLVED] %(title)s") % {"title": state.title})
        else:
            header = str(
                _("[%(severity)s] %(title)s")
                % {
                    "severity": state.get_severity_display(),
                    "title": state.title,
                }
            )
        lines = [
            header,
            str(_("项目: %(project)s") % {"project": state.project.name}),
            str(
                _("事件类型: %(event_type)s")
                % {"event_type": state.get_event_type_display()}
            ),
            str(
                _("对象: %(object_type)s #%(object_pk)s")
                % {"object_type": state.object_type, "object_pk": state.object_pk}
            ),
            str(_("详情: %(detail)s") % {"detail": state.detail}),
        ]
        if state.admin_url:
            lines.append(str(_("后台: %(url)s") % {"url": state.admin_url}))
        timestamp = state.resolved_at if mode == "resolved" else state.first_seen_at
        if timestamp is not None:
            label = _("恢复时间") if mode == "resolved" else _("发现时间")
            lines.append(
                str(
                    _("%(label)s: %(time)s")
                    % {
                        "label": label,
                        "time": timezone.localtime(timestamp).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                    }
                )
            )
        return "\n".join(lines)

    def _send_to_telegram(
        self, *, config: ProjectTelegramAlertConfig, text: str
    ) -> None:
        if not self.is_configured:
            raise TelegramAlertError("ALERTS_TELEGRAM_BOT_TOKEN 未配置")

        payload = {
            "chat_id": config.telegram_chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if config.telegram_thread_id:
            try:
                payload["message_thread_id"] = int(config.telegram_thread_id)
            except (ValueError, TypeError) as exc:
                raise TelegramAlertError(
                    f"telegram_thread_id 不是合法数字: {config.telegram_thread_id}"
                ) from exc

        try:
            response = httpx.post(
                f"{self._api_base}/bot{self._bot_token}/sendMessage",
                json=payload,
                timeout=self._timeout,
            )
            response.raise_for_status()
            response_payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramAlertError(str(exc)) from exc

        if not response_payload.get("ok", False):
            raise TelegramAlertError(str(response_payload))
