from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from chains.constants import ChainCode
from chains.models import Chain
from chains.tests_fixtures import make_evm_chain
from chains.tests_fixtures import make_tron_chain
from core.monitoring import SCAN_STALL_ALERT_AFTER_SECONDS


class HealthEndpointTests(TestCase):
    """/health 是容器编排的判活依据，其 200/503 契约必须锁死。

    这里测的不是"页面能打开"，而是行为正确性：任一硬依赖不可用时必须返回 503，
    否则 docker healthcheck 会把一个连不上数据库的实例标成 healthy，
    depends_on 门控与运维告警同时失效。
    """

    def test_returns_ok_when_dependencies_available(self):
        response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_returns_503_when_database_unavailable(self):
        with patch("core.health.connection.cursor", side_effect=OSError("db down")):
            response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unhealthy"})

    def test_returns_503_when_cache_unavailable(self):
        with patch("core.health.cache.set", side_effect=OSError("redis down")):
            response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unhealthy"})

    def test_returns_503_when_cache_write_silently_dropped(self):
        """写入不报错但读不回来（典型如 maxmemory 打满）同样必须判为不健康。"""
        with patch("core.health.cache.get", return_value=None):
            response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unhealthy"})

    def test_response_body_leaks_no_internal_detail(self):
        """端点无鉴权，响应体只允许出现 status 字段。"""
        with patch("core.health.connection.cursor", side_effect=OSError("db down")):
            response = self.client.get(reverse("health"))

        self.assertEqual(list(response.json().keys()), ["status"])


class ScanningHealthEndpointTests(TestCase):
    """/health/scanning 是「扫描还在不在转」的唯一外部信号，其 200/503 契约必须锁死。

    Celery worker 死亡时，跑在它内部的巡检任务会一起停摆，无法自我上报。该端点由
    django 进程回答，故障域与被监控对象分离，使扫描停摆能在一个探测周期内被发现。
    """

    def stall(self, chain):
        """把链的 last_scanned_at 推到停滞阈值之外。

        last_scanned_at 是 auto_now_add 字段，创建后不可经 save() 改写，
        只能像 Chain.mark_scanned() 那样走 queryset.update()。
        """
        Chain.objects.filter(pk=chain.pk).update(
            last_scanned_at=timezone.now()
            - timedelta(seconds=SCAN_STALL_ALERT_AFTER_SECONDS + 60)
        )

    def test_returns_ok_when_chains_recently_scanned(self):
        make_evm_chain(code=ChainCode.Anvil)
        make_tron_chain()

        response = self.client.get(reverse("health-scanning"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_returns_ok_when_no_active_chain_exists(self):
        # 没有活跃链时无扫描可言，不能报警——否则全新部署会永远红着。
        response = self.client.get(reverse("health-scanning"))

        self.assertEqual(response.status_code, 200)

    def test_returns_503_when_evm_chain_scan_stalled(self):
        self.stall(make_evm_chain(code=ChainCode.Anvil))

        response = self.client.get(reverse("health-scanning"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "stalled"})

    def test_returns_503_when_tron_chain_scan_stalled(self):
        self.stall(make_tron_chain())

        self.assertEqual(self.client.get(reverse("health-scanning")).status_code, 503)

    def test_inactive_chain_never_triggers_alert(self):
        # 停用的链本就不参与扫描调度，其 last_scanned_at 会一直停在停用时刻。
        self.stall(make_evm_chain(code=ChainCode.Anvil, active=False))

        self.assertEqual(self.client.get(reverse("health-scanning")).status_code, 200)

    def test_response_body_leaks_no_internal_detail(self):
        # 端点无鉴权，停滞链的 code 只允许出现在结构化日志里，不能进响应体。
        self.stall(make_evm_chain(code=ChainCode.Anvil))

        payload = self.client.get(reverse("health-scanning")).json()

        self.assertEqual(list(payload), ["status"])
