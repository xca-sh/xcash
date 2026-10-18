"""商户中心账单 / 充值列表的关键字、公链币种、创建时间筛选。"""

import csv
import io
from datetime import timedelta
from decimal import Decimal
from itertools import count

import pytest
from django.utils import timezone
from web3 import Web3

from chains.constants import ChainCode
from chains.models import Transfer
from chains.models import TransferStatus
from chains.tests_fixtures import make_evm_chain
from currencies.models import Crypto
from currencies.models import Fiat
from deposits.models import Deposit
from invoices.models import Invoice
from invoices.models import InvoiceStatus
from projects.models import Customer
from projects.models import Project

AUTH = "Bearer test-saas-token"
CAROL_ADDRESS = Web3.to_checksum_address("0x00000000000000000000000000000000000000de")
SEQ = count(1)


def make_transfer(chain, crypto, *, status=TransferStatus.CONFIRMED, from_address=None):
    n = next(SEQ)
    return Transfer.objects.create(
        chain=chain,
        block=n,
        block_hash="0x" + "aa" * 32,
        hash="0x" + f"{n:064x}",
        crypto=crypto,
        from_address=from_address or "0x00000000000000000000000000000000000000B1",
        to_address="0x00000000000000000000000000000000000000C1",
        value=Decimal("1"),
        amount=Decimal("1"),
        status=status,
        timestamp=int(timezone.now().timestamp()),
        datetime=timezone.now(),
    )


@pytest.fixture
def env():
    Fiat.objects.get_or_create(code="USD")
    project = Project.objects.create(name="list-filters")
    eth = make_evm_chain(code=ChainCode.Ethereum)
    bsc = make_evm_chain(code=ChainCode.BSC)
    usdt = Crypto.objects.create(
        name="Tether", symbol="USDT", prices={"USD": "1"}, coingecko_id="tether-lf"
    )
    usdc = Crypto.objects.create(
        name="USD Coin", symbol="USDC", prices={"USD": "1"}, coingecko_id="usdc-lf"
    )
    return {"project": project, "eth": eth, "bsc": bsc, "usdt": usdt, "usdc": usdc}


def get_json(client, path, **params):
    return client.get(path, params, HTTP_AUTHORIZATION=AUTH)


@pytest.mark.django_db
class TestInvoiceListFilters:
    @pytest.fixture
    def invoices(self, env):
        def create(
            out_no, title, chain, crypto, *, status=InvoiceStatus.WAITING, transfer=None
        ):
            invoice = Invoice.objects.create(
                project=env["project"],
                out_no=out_no,
                title=title,
                currency_id="USD",
                amount=Decimal("10"),
                methods={},
                chain=chain,
                crypto=crypto,
                expires_at=timezone.now(),
                status=status,
            )
            if transfer is not None:
                Invoice.objects.filter(pk=invoice.pk).update(transfer=transfer)
            return invoice

        paid_tx = make_transfer(env["eth"], env["usdt"])
        return {
            "a": create(
                "ORDER-A1",
                "Pro plan",
                env["eth"],
                env["usdt"],
                status=InvoiceStatus.COMPLETED,
                transfer=paid_tx,
            ),
            "b": create("ORDER-B2", "Basic plan", env["bsc"], env["usdt"]),
            "c": create("REFUND-C3", "Top up credits", env["bsc"], env["usdc"]),
            "paid_tx": paid_tx,
        }

    def list_out_nos(self, client, env, **params):
        resp = get_json(
            client, f"/saas/v1/projects/{env['project'].appid}/invoices", **params
        )
        assert resp.status_code == 200, resp.content
        return {row["out_no"] for row in resp.json()["results"]}

    def test_keyword_matches_out_no_title_and_sys_no(self, client, env, invoices):
        assert self.list_out_nos(client, env, q="order") == {"ORDER-A1", "ORDER-B2"}
        assert self.list_out_nos(client, env, q="credits") == {"REFUND-C3"}
        sys_no = invoices["b"].sys_no
        assert self.list_out_nos(client, env, q=sys_no) == {"ORDER-B2"}

    def test_keyword_matches_tx_hash_case_insensitively(self, client, env, invoices):
        tx_hash = invoices["paid_tx"].hash.upper().replace("0X", "0x")
        assert self.list_out_nos(client, env, q=tx_hash) == {"ORDER-A1"}

    def test_chain_and_crypto_filters_combine(self, client, env, invoices):
        assert self.list_out_nos(client, env, chain="bsc") == {"ORDER-B2", "REFUND-C3"}
        assert self.list_out_nos(client, env, chain="bsc", crypto="usdc") == {
            "REFUND-C3"
        }

    def test_keyword_combines_with_status(self, client, env, invoices):
        assert self.list_out_nos(client, env, q="order", status="waiting") == {
            "ORDER-B2"
        }

    def test_created_range_is_half_open(self, client, env, invoices):
        old = timezone.now() - timedelta(days=10)
        Invoice.objects.filter(pk=invoices["c"].pk).update(created_at=old)
        boundary = (old + timedelta(days=1)).isoformat()
        assert self.list_out_nos(client, env, created_before=boundary) == {"REFUND-C3"}
        assert self.list_out_nos(client, env, created_after=boundary) == {
            "ORDER-A1",
            "ORDER-B2",
        }

    def test_naive_datetime_treated_as_utc(self, client, env, invoices):
        old = timezone.now() - timedelta(days=10)
        Invoice.objects.filter(pk=invoices["c"].pk).update(created_at=old)
        naive = (old + timedelta(days=1)).replace(tzinfo=None).isoformat()
        assert self.list_out_nos(client, env, created_before=naive) == {"REFUND-C3"}

    def test_invalid_datetime_returns_400(self, client, env, invoices):
        resp = get_json(
            client,
            f"/saas/v1/projects/{env['project'].appid}/invoices",
            created_after="yesterday",
        )
        assert resp.status_code == 400

    def test_reversed_range_returns_400(self, client, env, invoices):
        now = timezone.now()
        resp = get_json(
            client,
            f"/saas/v1/projects/{env['project'].appid}/invoices",
            created_after=now.isoformat(),
            created_before=(now - timedelta(days=1)).isoformat(),
        )
        assert resp.status_code == 400

    def test_status_counts_follow_same_filters(self, client, env, invoices):
        resp = get_json(
            client,
            f"/saas/v1/projects/{env['project'].appid}/stats/invoice-status-counts",
            q="order",
        )
        counts = resp.json()
        assert counts["total"] == 2
        assert counts["completed"] == 1
        assert counts["waiting"] == 1

    def test_filters_do_not_leak_other_projects(self, client, env, invoices):
        other = Project.objects.create(name="other")
        Invoice.objects.create(
            project=other,
            out_no="ORDER-X",
            title="x",
            currency_id="USD",
            amount=Decimal("1"),
            methods={},
            expires_at=timezone.now(),
        )
        assert self.list_out_nos(client, env, q="order") == {"ORDER-A1", "ORDER-B2"}


@pytest.mark.django_db
class TestDepositListFilters:
    @pytest.fixture
    def deposits(self, env):
        def create(
            uid, chain, crypto, *, status=TransferStatus.CONFIRMED, from_address=None
        ):
            customer, _ = Customer.objects.get_or_create(
                project=env["project"], uid=uid
            )
            transfer = make_transfer(
                chain, crypto, status=status, from_address=from_address
            )
            return Deposit.objects.create(
                customer=customer, transfer=transfer, worth=Decimal("1")
            )

        return {
            "a": create("user-alice", env["eth"], env["usdt"]),
            "b": create(
                "user-bob", env["bsc"], env["usdt"], status=TransferStatus.CONFIRMING
            ),
            "c": create(
                "vip-carol",
                env["bsc"],
                env["usdc"],
                from_address=CAROL_ADDRESS,
            ),
        }

    def list_uids(self, client, env, **params):
        resp = get_json(
            client, f"/saas/v1/projects/{env['project'].appid}/deposits", **params
        )
        assert resp.status_code == 200, resp.content
        return {row["uid"] for row in resp.json()["results"]}

    def test_status_filter(self, client, env, deposits):
        assert self.list_uids(client, env, status="confirming") == {"user-bob"}
        assert self.list_uids(client, env, status="confirmed") == {
            "user-alice",
            "vip-carol",
        }

    def test_unknown_status_returns_400(self, client, env, deposits):
        resp = get_json(
            client, f"/saas/v1/projects/{env['project'].appid}/deposits", status="paid"
        )
        assert resp.status_code == 400

    def test_keyword_matches_uid_sys_no_and_from_address(self, client, env, deposits):
        assert self.list_uids(client, env, q="user-") == {"user-alice", "user-bob"}
        assert self.list_uids(client, env, q=deposits["c"].sys_no) == {"vip-carol"}
        # 地址落库为 checksum 形式，商户粘贴全小写也应命中
        assert self.list_uids(client, env, q=CAROL_ADDRESS.lower()) == {"vip-carol"}

    def test_keyword_matches_tx_hash(self, client, env, deposits):
        assert self.list_uids(client, env, q=deposits["a"].transfer.hash) == {
            "user-alice"
        }

    def test_chain_and_crypto_filters(self, client, env, deposits):
        assert self.list_uids(client, env, chain="bsc") == {"user-bob", "vip-carol"}
        assert self.list_uids(client, env, crypto="USDC") == {"vip-carol"}

    def test_created_range(self, client, env, deposits):
        old = timezone.now() - timedelta(days=10)
        Deposit.objects.filter(pk=deposits["a"].pk).update(created_at=old)
        boundary = (old + timedelta(days=1)).isoformat()
        assert self.list_uids(client, env, created_before=boundary) == {"user-alice"}
        assert self.list_uids(client, env, created_after=boundary) == {
            "user-bob",
            "vip-carol",
        }

    def test_detail_ignores_list_filters(self, client, env, deposits):
        sys_no = deposits["a"].sys_no
        resp = get_json(
            client,
            f"/saas/v1/projects/{env['project'].appid}/deposits/{sys_no}",
            status="confirming",
        )
        assert resp.status_code == 200


def export_rows(client, path, **params):
    resp = get_json(client, path, **params)
    assert resp.status_code == 200, resp.content
    assert resp["Content-Type"].startswith("text/csv")
    assert resp["Content-Disposition"].startswith("attachment;")
    raw = b"".join(resp.streaming_content).decode("utf-8")
    # 开头带 BOM，Excel 打开不乱码
    assert raw.startswith("﻿")
    return list(csv.DictReader(io.StringIO(raw[1:])))


@pytest.mark.django_db
class TestInvoiceExport:
    @pytest.fixture
    def invoices(self, env):
        def create(out_no, title, chain, crypto, *, status=InvoiceStatus.WAITING):
            return Invoice.objects.create(
                project=env["project"],
                out_no=out_no,
                title=title,
                currency_id="USD",
                amount=Decimal("10.50"),
                methods={},
                chain=chain,
                crypto=crypto,
                expires_at=timezone.now(),
                status=status,
            )

        paid = create(
            "ORDER-A1",
            "Pro plan",
            env["eth"],
            env["usdt"],
            status=InvoiceStatus.COMPLETED,
        )
        Invoice.objects.filter(pk=paid.pk).update(
            transfer=make_transfer(env["eth"], env["usdt"])
        )
        confirming = create("ORDER-B2", "Basic plan", env["bsc"], env["usdt"])
        Invoice.objects.filter(pk=confirming.pk).update(
            transfer=make_transfer(
                env["bsc"], env["usdt"], status=TransferStatus.CONFIRMING
            )
        )
        create("=HYPERLINK(1)", "@SUM(A1)", env["bsc"], env["usdc"])
        return {"paid": paid, "confirming": confirming}

    def path(self, env):
        return f"/saas/v1/projects/{env['project'].appid}/invoices/export"

    def test_export_follows_list_filters(self, client, env, invoices):
        rows = export_rows(client, self.path(env), q="order", chain="bsc")
        assert [row["out_no"] for row in rows] == ["ORDER-B2"]

    def test_export_uses_display_status(self, client, env, invoices):
        rows = {row["sys_no"]: row for row in export_rows(client, self.path(env))}
        assert rows[invoices["confirming"].sys_no]["status"] == "confirming"
        assert rows[invoices["paid"].sys_no]["status"] == "completed"
        assert rows[invoices["paid"].sys_no]["amount"] == "10.5"
        assert rows[invoices["paid"].sys_no]["tx_hash"].startswith("0x")
        confirming_rows = export_rows(client, self.path(env), status="confirming")
        assert [row["sys_no"] for row in confirming_rows] == [
            invoices["confirming"].sys_no
        ]

    def test_export_escapes_formula_cells(self, client, env, invoices):
        rows = export_rows(client, self.path(env), crypto="USDC")
        assert rows[0]["out_no"] == "'=HYPERLINK(1)"
        assert rows[0]["title"] == "'@SUM(A1)"

    def test_export_does_not_leak_other_projects(self, client, env, invoices):
        other = Project.objects.create(name="other-export")
        Invoice.objects.create(
            project=other,
            out_no="ORDER-X",
            title="x",
            currency_id="USD",
            amount=Decimal("1"),
            methods={},
            expires_at=timezone.now(),
        )
        out_nos = {row["out_no"] for row in export_rows(client, self.path(env))}
        assert "ORDER-X" not in out_nos
        assert len(out_nos) == 3

    def test_export_rejects_invalid_filters(self, client, env, invoices):
        resp = get_json(client, self.path(env), created_after="yesterday")
        assert resp.status_code == 400
        resp = get_json(client, self.path(env), status="paid")
        assert resp.status_code == 400

    def test_export_over_limit_returns_400(self, client, env, invoices, monkeypatch):
        monkeypatch.setattr("saas_api.csv_export.EXPORT_MAX_ROWS", 2)
        resp = get_json(client, self.path(env))
        assert resp.status_code == 400
        assert "缩小时间范围" in resp.json()["detail"]
        # 筛选后不超上限即可导出
        assert len(export_rows(client, self.path(env), chain="bsc")) == 2

    def test_export_requires_token(self, client, env, invoices):
        resp = client.get(self.path(env))
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestDepositExport:
    @pytest.fixture
    def deposits(self, env):
        def create(uid, chain, crypto, *, status=TransferStatus.CONFIRMED):
            customer, _ = Customer.objects.get_or_create(
                project=env["project"], uid=uid
            )
            return Deposit.objects.create(
                customer=customer,
                transfer=make_transfer(chain, crypto, status=status),
                worth=Decimal("1.250000"),
            )

        return {
            "a": create("user-alice", env["eth"], env["usdt"]),
            "b": create(
                "user-bob", env["bsc"], env["usdt"], status=TransferStatus.CONFIRMING
            ),
            "c": create("-cmd|calc", env["bsc"], env["usdc"]),
        }

    def path(self, env):
        return f"/saas/v1/projects/{env['project'].appid}/deposits/export"

    def test_export_follows_list_filters(self, client, env, deposits):
        rows = export_rows(client, self.path(env), status="confirmed", chain="bsc")
        assert [row["sys_no"] for row in rows] == [deposits["c"].sys_no]
        rows = export_rows(client, self.path(env), q="user-", status="confirming")
        assert [row["uid"] for row in rows] == ["user-bob"]

    def test_export_row_content(self, client, env, deposits):
        rows = {row["sys_no"]: row for row in export_rows(client, self.path(env))}
        row = rows[deposits["a"].sys_no]
        assert row["uid"] == "user-alice"
        assert row["status"] == "confirmed"
        assert (row["chain"], row["crypto"]) == ("ethereum", "USDT")
        assert row["worth"] == "1.25"
        assert row["tx_hash"] == deposits["a"].transfer.hash
        # 文本列做公式注入转义
        assert rows[deposits["c"].sys_no]["uid"] == "'-cmd|calc"

    def test_export_does_not_leak_other_projects(self, client, env, deposits):
        other = Project.objects.create(name="other-deposit-export")
        customer = Customer.objects.create(project=other, uid="user-mallory")
        Deposit.objects.create(
            customer=customer,
            transfer=make_transfer(env["eth"], env["usdt"]),
            worth=Decimal("1"),
        )
        uids = {row["uid"] for row in export_rows(client, self.path(env))}
        assert "user-mallory" not in uids
        assert len(uids) == 3

    def test_export_over_limit_returns_400(self, client, env, deposits, monkeypatch):
        monkeypatch.setattr("saas_api.csv_export.EXPORT_MAX_ROWS", 1)
        resp = get_json(client, self.path(env))
        assert resp.status_code == 400
        assert len(export_rows(client, self.path(env), status="confirming")) == 1

    def test_export_does_not_shadow_detail_route(self, client, env, deposits):
        sys_no = deposits["a"].sys_no
        resp = get_json(
            client, f"/saas/v1/projects/{env['project'].appid}/deposits/{sys_no}"
        )
        assert resp.status_code == 200
