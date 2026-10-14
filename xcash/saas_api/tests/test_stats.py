"""saas_api /projects/{appid}/stats/ 路由与鉴权。"""

import itertools
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from chains.constants import ChainCode
from chains.models import Transfer
from chains.models import TransferStatus
from chains.tests_fixtures import make_evm_chain
from currencies.models import Crypto
from currencies.models import Fiat
from invoices.models import Invoice
from invoices.models import InvoiceStatus
from projects.models import Project

AUTH = "Bearer test-saas-token"


@pytest.fixture
def project(db):
    return Project.objects.create(name="stats-test")


@pytest.mark.django_db
class TestStatsRouting:
    def _url(self, project, action):
        return f"/saas/v1/projects/{project.appid}/stats/{action}"

    def test_summary_requires_auth(self, client, project):
        resp = client.get(
            self._url(project, "summary")
            + "?cur_start=2026-04-01T00:00:00Z&cur_end=2026-04-18T00:00:00Z"
            + "&prev_start=2026-03-01T00:00:00Z&prev_end=2026-03-18T00:00:00Z"
        )
        assert resp.status_code in (401, 403)

    def test_summary_happy_path_empty(self, client, project):
        resp = client.get(
            self._url(project, "summary")
            + "?cur_start=2026-04-01T00:00:00Z&cur_end=2026-04-18T00:00:00Z"
            + "&prev_start=2026-03-01T00:00:00Z&prev_end=2026-03-18T00:00:00Z",
            HTTP_AUTHORIZATION=AUTH,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["gmv_usd"] == "0.000000"
        assert body["prev_gmv_usd"] == "0.000000"
        assert body["invoice_count"] == 0
        assert body["prev_invoice_count"] == 0
        assert body["completed_invoice_count"] == 0


# ─── 辅助函数 ────────────────────────────────────────────────────────────────

# 用于生成唯一的商户单号，避免 project+out_no 唯一约束冲突
_out_no_counter = itertools.count(1)


def _make_invoice(project, *, worth, status, started_at, updated_at):
    """创建一张账单并覆盖 auto 字段。Invoice.started_at 是 auto_now_add，
    updated_at 是 auto_now，需要用 QuerySet.update() 绕过才能控制测试时间。"""
    Fiat.objects.get_or_create(code="USD")
    inv = Invoice.objects.create(
        project=project,
        worth=Decimal(worth),
        status=status,
        expires_at=started_at,  # 字段 non-null，给个占位值
        out_no=f"TEST-{next(_out_no_counter)}",
        title="Test Invoice",
        currency_id="USD",
        amount=Decimal(worth),
    )
    Invoice.objects.filter(pk=inv.pk).update(
        started_at=started_at,
        updated_at=updated_at,
    )
    inv.refresh_from_db()
    return inv


# ─── 聚合正确性测试 ───────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestStatsSummaryAggregation:
    CUR_START = "2026-04-01T00:00:00Z"
    CUR_END = "2026-04-18T00:00:00Z"
    PREV_START = "2026-03-01T00:00:00Z"
    PREV_END = "2026-03-18T00:00:00Z"

    def _url(self, project):
        qs = (
            f"?cur_start={self.CUR_START}&cur_end={self.CUR_END}"
            f"&prev_start={self.PREV_START}&prev_end={self.PREV_END}"
        )
        return f"/saas/v1/projects/{project.appid}/stats/summary{qs}"

    def test_gmv_sums_completed_invoices_in_window(self, client, project):
        # 当期 4 月 completed → 计入
        _make_invoice(
            project,
            worth="100.50",
            status=InvoiceStatus.COMPLETED,
            started_at=datetime(2026, 4, 5, tzinfo=UTC),
            updated_at=datetime(2026, 4, 10, tzinfo=UTC),
        )
        _make_invoice(
            project,
            worth="200.25",
            status=InvoiceStatus.COMPLETED,
            started_at=datetime(2026, 4, 6, tzinfo=UTC),
            updated_at=datetime(2026, 4, 15, tzinfo=UTC),
        )
        # waiting 不计入 GMV
        _make_invoice(
            project,
            worth="999.00",
            status=InvoiceStatus.WAITING,
            started_at=datetime(2026, 4, 7, tzinfo=UTC),
            updated_at=datetime(2026, 4, 7, tzinfo=UTC),
        )
        # 上期 3 月 completed → 计入 prev
        _make_invoice(
            project,
            worth="50.00",
            status=InvoiceStatus.COMPLETED,
            started_at=datetime(2026, 3, 5, tzinfo=UTC),
            updated_at=datetime(2026, 3, 10, tzinfo=UTC),
        )

        resp = client.get(self._url(project), HTTP_AUTHORIZATION=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert Decimal(body["gmv_usd"]) == Decimal("300.75")
        assert Decimal(body["prev_gmv_usd"]) == Decimal("50.00")

    def test_invoice_count_uses_started_at_all_status(self, client, project):
        # 所有账单状态均在当期窗口内（按 started_at），全部计入 invoice_count
        for i, status in enumerate(
            [
                InvoiceStatus.WAITING,
                InvoiceStatus.COMPLETED,
                InvoiceStatus.EXPIRED,
            ]
        ):
            _make_invoice(
                project,
                worth="1.00",
                status=status,
                started_at=datetime(2026, 4, 2 + i, tzinfo=UTC),
                updated_at=datetime(2026, 4, 2 + i, tzinfo=UTC),
            )
        # 上期 3 月 1 张 completed
        _make_invoice(
            project,
            worth="1.00",
            status=InvoiceStatus.COMPLETED,
            started_at=datetime(2026, 3, 5, tzinfo=UTC),
            updated_at=datetime(2026, 3, 5, tzinfo=UTC),
        )

        resp = client.get(self._url(project), HTTP_AUTHORIZATION=AUTH).json()
        assert resp["invoice_count"] == 3
        assert resp["prev_invoice_count"] == 1
        # completed_invoice_count 只数当期 updated_at 在窗口内的 completed 状态账单
        assert resp["completed_invoice_count"] == 1

    def test_boundaries_are_half_open(self, client, project):
        # 正好 cur_start 的账单应被包含，正好 cur_end 的应被排除
        _make_invoice(
            project,
            worth="10.00",
            status=InvoiceStatus.COMPLETED,
            started_at=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            updated_at=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        )
        _make_invoice(
            project,
            worth="20.00",
            status=InvoiceStatus.COMPLETED,
            started_at=datetime(2026, 4, 18, 0, 0, tzinfo=UTC),
            updated_at=datetime(2026, 4, 18, 0, 0, tzinfo=UTC),
        )
        resp = client.get(self._url(project), HTTP_AUTHORIZATION=AUTH).json()
        # GMV 只计 updated_at 在 [cur_start, cur_end) 内的，cur_end 边界被排除
        assert Decimal(resp["gmv_usd"]) == Decimal("10.00")

    def test_invalid_timestamp_returns_400(self, client, project):
        url = (
            f"/saas/v1/projects/{project.appid}/stats/summary"
            "?cur_start=not-a-date&cur_end=2026-04-18T00:00:00Z"
            "&prev_start=2026-03-01T00:00:00Z&prev_end=2026-03-18T00:00:00Z"
        )
        resp = client.get(url, HTTP_AUTHORIZATION=AUTH)
        assert resp.status_code == 400

    def test_missing_timestamp_returns_400(self, client, project):
        url = (
            f"/saas/v1/projects/{project.appid}/stats/summary"
            "?cur_end=2026-04-18T00:00:00Z"
            "&prev_start=2026-03-01T00:00:00Z&prev_end=2026-03-18T00:00:00Z"
        )
        resp = client.get(url, HTTP_AUTHORIZATION=AUTH)
        assert resp.status_code == 400

    def test_unknown_project_returns_not_found(self, client):
        url = (
            "/saas/v1/projects/nonexistent-appid/stats/summary"
            "?cur_start=2026-04-01T00:00:00Z&cur_end=2026-04-18T00:00:00Z"
            "&prev_start=2026-03-01T00:00:00Z&prev_end=2026-03-18T00:00:00Z"
        )
        resp = client.get(url, HTTP_AUTHORIZATION=AUTH)
        assert resp.status_code == 404


# ─── daily 每日时序测试 ────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestStatsDaily:
    def _url(self, project, days):
        return f"/saas/v1/projects/{project.appid}/stats/daily?days={days}&metric=gmv"

    def test_rejects_unknown_days(self, client, project):
        resp = client.get(self._url(project, 42), HTTP_AUTHORIZATION=AUTH)
        assert resp.status_code == 400

    def test_rejects_unknown_metric(self, client, project):
        resp = client.get(
            f"/saas/v1/projects/{project.appid}/stats/daily?days=7&metric=foo",
            HTTP_AUTHORIZATION=AUTH,
        )
        assert resp.status_code == 400

    def test_empty_series_fills_zero_for_each_day(self, client, project):
        resp = client.get(self._url(project, 7), HTTP_AUTHORIZATION=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["metric"] == "gmv"
        assert body["days"] == 7
        assert len(body["series"]) == 7
        for item in body["series"]:
            assert item["value"] == "0.000000"
            assert len(item["date"]) == 10  # YYYY-MM-DD

    def test_groups_completed_invoices_by_utc_day(self, client, project):
        from django.utils import timezone as django_tz

        # 用相对"今天"的日期，避免测试在未来某日过期
        today = django_tz.now().astimezone(UTC).date()
        base = datetime(
            today.year, today.month, today.day, 10, 0, tzinfo=UTC
        ) - timedelta(days=3)

        for offset, worth in [(0, "100.00"), (1, "50.50"), (2, "25.00")]:
            _make_invoice(
                project,
                worth=worth,
                status=InvoiceStatus.COMPLETED,
                started_at=base + timedelta(days=offset),
                updated_at=base + timedelta(days=offset),
            )
        resp = client.get(self._url(project, 7), HTTP_AUTHORIZATION=AUTH).json()
        by_date = {item["date"]: Decimal(item["value"]) for item in resp["series"]}

        expected_dates = [
            (base + timedelta(days=i)).date().isoformat() for i in range(3)
        ]
        assert by_date.get(expected_dates[0]) == Decimal("100.00")
        assert by_date.get(expected_dates[1]) == Decimal("50.50")
        assert by_date.get(expected_dates[2]) == Decimal("25.00")


# ─── 状态计数 ─────────────────────────────────────────────────────────────────

_hash_counter = itertools.count(1)


def _bind_transfer(invoice, *, chain, crypto, status):
    """给账单挂一笔链上转账，模拟「付款已识别、等待确认」或「已确认」。"""
    now = timezone.now()
    transfer = Transfer.objects.create(
        chain=chain,
        block=1,
        block_hash="0x" + "aa" * 32,
        hash=f"0x{next(_hash_counter):064x}",
        crypto=crypto,
        from_address="0x00000000000000000000000000000000000000B1",
        to_address="0x00000000000000000000000000000000000000C1",
        value=Decimal("1"),
        amount=Decimal("1"),
        status=status,
        timestamp=int(now.timestamp()),
        datetime=now,
    )
    Invoice.objects.filter(pk=invoice.pk).update(transfer=transfer)
    return transfer


@pytest.mark.django_db
class TestInvoiceStatusCounts:
    def _url(self, project):
        return f"/saas/v1/projects/{project.appid}/stats/invoice-status-counts"

    @pytest.fixture
    def chain(self, db):
        return make_evm_chain(code=ChainCode.Ethereum)

    @pytest.fixture
    def crypto(self, db):
        return Crypto.objects.create(
            name="Tether",
            symbol="USDT",
            prices={"USD": "1"},
            coingecko_id="tether-stats-test",
        )

    def _invoice(self, project, status):
        at = datetime(2026, 4, 5, tzinfo=UTC)
        return _make_invoice(
            project, worth="10", status=status, started_at=at, updated_at=at
        )

    def test_requires_auth(self, client, project):
        resp = client.get(self._url(project))
        assert resp.status_code in (401, 403)

    def test_unknown_project_returns_error(self, client):
        resp = client.get(
            "/saas/v1/projects/XC-NOT-EXIST/stats/invoice-status-counts",
            HTTP_AUTHORIZATION=AUTH,
        )
        assert resp.status_code != 200

    def test_confirming_is_derived_from_bound_confirming_transfer(
        self, client, project, chain, crypto
    ):
        # 纯待支付 ×2：未绑定转账
        self._invoice(project, InvoiceStatus.WAITING)
        self._invoice(project, InvoiceStatus.WAITING)
        # 待支付但链上付款确认中 → 计入 confirming
        _bind_transfer(
            self._invoice(project, InvoiceStatus.WAITING),
            chain=chain,
            crypto=crypto,
            status=TransferStatus.CONFIRMING,
        )
        # 已超时但截止前付的款仍在确认中 → 同样计入 confirming
        _bind_transfer(
            self._invoice(project, InvoiceStatus.EXPIRED),
            chain=chain,
            crypto=crypto,
            status=TransferStatus.CONFIRMING,
        )
        # 纯已超时
        self._invoice(project, InvoiceStatus.EXPIRED)
        # 已完成且转账已确认
        _bind_transfer(
            self._invoice(project, InvoiceStatus.COMPLETED),
            chain=chain,
            crypto=crypto,
            status=TransferStatus.CONFIRMED,
        )
        # 其他项目的账单不得计入
        other = Project.objects.create(name="stats-other")
        self._invoice(other, InvoiceStatus.WAITING)

        resp = client.get(self._url(project), HTTP_AUTHORIZATION=AUTH)

        assert resp.status_code == 200
        assert resp.json() == {
            "total": 6,
            "waiting": 2,
            "confirming": 2,
            "completed": 1,
            "expired": 1,
        }
