"""internal_api 列表端点的分页行为验证。

验证通过全局 REST_FRAMEWORK.DEFAULT_PAGINATION_CLASS 接入的
common.pagination.PageNumberSizePagination 真的作用于内网 API 列表端点：
- 支持 size 查询参数覆盖
- 支持 page 查询参数翻页
- 响应结构包含 {count, next, previous, results}

使用 webhook-events 端点作为载体：它是 ListModelMixin + GenericViewSet，
依赖最少（仅 Project + WebhookEvent），是验证分页的典型用例。
"""

import pytest
from internal_api.viewsets.deposits import InternalDepositViewSet
from internal_api.viewsets.differ_recipient_addresses import (
    DifferRecipientAddressViewSet,
)
from internal_api.viewsets.operations import HotWalletFundingViewSet
from internal_api.viewsets.withdrawals import InternalWithdrawalViewSet

from chains.models import Wallet
from common.pagination import PageNumberSizePagination
from projects.models import Project
from webhooks.models import WebhookEvent

AUTH_HEADER = "Bearer test-internal-token"


@pytest.fixture
def project(db):
    # 本地创建 Wallet 记录即可，不走 Wallet.generate()（那会调用远端 signer）。
    wallet = Wallet.objects.create()
    return Project.objects.create(name="pagination-test-project", wallet=wallet)


@pytest.fixture
def webhook_events(project):
    # 创建足够多的事件，用于覆盖 size 参数和翻页行为。
    events = [
        WebhookEvent(
            project=project,
            payload={"index": i},
            status=WebhookEvent.Status.PENDING,
        )
        for i in range(25)
    ]
    WebhookEvent.objects.bulk_create(events)
    return WebhookEvent.objects.filter(project=project)


@pytest.mark.django_db
class TestInternalApiPagination:
    """通过 webhook-events 端点验证全局分页实际生效。"""

    endpoint_template = "/internal/v1/projects/{appid}/webhook-events"

    def _url(self, project):
        return self.endpoint_template.format(appid=project.appid)

    def test_response_has_pagination_envelope(self, client, project, webhook_events):
        response = client.get(self._url(project), HTTP_AUTHORIZATION=AUTH_HEADER)
        assert response.status_code == 200
        body = response.json()
        # 至少这四个字段，前端分页 UI 依赖它们。
        assert set(body.keys()) >= {"count", "next", "previous", "results"}
        assert body["count"] == 25
        assert body["previous"] is None

    def test_size_query_param_overrides_default(self, client, project, webhook_events):
        response = client.get(
            self._url(project) + "?size=5", HTTP_AUTHORIZATION=AUTH_HEADER
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["results"]) == 5
        assert body["count"] == 25
        assert body["next"] is not None

    def test_page_query_param_paginates(self, client, project, webhook_events):
        first = client.get(
            self._url(project) + "?page=1&size=5", HTTP_AUTHORIZATION=AUTH_HEADER
        ).json()
        second = client.get(
            self._url(project) + "?page=2&size=5", HTTP_AUTHORIZATION=AUTH_HEADER
        ).json()
        assert len(second["results"]) == 5
        # 第 2 页上一页应当指向第 1 页（且不能是 None）。
        assert second["previous"] is not None
        # 分页应返回不同记录，而非重复第一页。
        first_ids = {row["id"] for row in first["results"]}
        second_ids = {row["id"] for row in second["results"]}
        assert first_ids.isdisjoint(second_ids)
        # 遍历所有页，验证：任意两页 ID 不重叠，且所有页的记录数之和 = count。
        all_ids: set[int] = set()
        total_seen = 0
        page = 1
        while True:
            body = client.get(
                self._url(project) + f"?page={page}&size=5",
                HTTP_AUTHORIZATION=AUTH_HEADER,
            ).json()
            page_ids = {row["id"] for row in body["results"]}
            # 关键断言：稳定排序保证任何两页之间 ID 集合不重叠。
            assert all_ids.isdisjoint(page_ids), (
                f"page {page} 与之前页存在重复 ID，说明排序不稳定"
            )
            all_ids.update(page_ids)
            total_seen += len(body["results"])
            if body["next"] is None:
                break
            page += 1
        assert total_seen == body["count"]

    def test_oversized_page_size_is_capped(self, client, project):
        max_page_size = PageNumberSizePagination.max_page_size
        events = [
            WebhookEvent(
                project=project,
                payload={"index": i},
                status=WebhookEvent.Status.PENDING,
            )
            for i in range(max_page_size + 1)
        ]
        WebhookEvent.objects.bulk_create(events)

        response = client.get(
            self._url(project) + f"?size={max_page_size * 10}",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["count"] == max_page_size + 1
        assert len(body["results"]) == max_page_size
        assert body["next"] is not None


@pytest.mark.django_db
class TestListViewSetsHaveStableOrdering:
    """验证列表 ViewSet 的 queryset 都有显式稳定排序。

    没有显式 ordering 时，DRF 会发出 UnorderedObjectListWarning；
    更严重的是分页在高并发下可能出现重复或缺失记录。
    本测试针对仍存在的列表 ViewSet。
    """

    @pytest.mark.parametrize(
        "viewset_cls",
        [
            InternalDepositViewSet,
            InternalWithdrawalViewSet,
            HotWalletFundingViewSet,
            DifferRecipientAddressViewSet,
        ],
    )
    def test_queryset_is_ordered(self, viewset_cls):
        """queryset.ordered 为 True 代表已有 ORDER BY 子句。"""
        viewset = viewset_cls()
        # get_queryset 依赖 self.kwargs["project_appid"]；
        # 任意字符串都行，queryset 的 .ordered 只取决于 .order_by(...) 的存在。
        viewset.kwargs = {"project_appid": "nonexistent-appid"}
        queryset = viewset.get_queryset()
        assert queryset.ordered, (
            f"{viewset_cls.__name__}.get_queryset() 返回的 queryset 没有显式 ordering，"
            f"分页时会产生 UnorderedObjectListWarning 及可能的重复/缺失记录"
        )
