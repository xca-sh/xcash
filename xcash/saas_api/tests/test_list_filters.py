"""商户中心账单 / 充值列表的关键字、公链币种、创建时间筛选。"""

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
