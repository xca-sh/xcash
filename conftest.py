"""让 pytest 与 manage.py 共享同一套应用导入路径。"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent
APPS_DIR = PROJECT_ROOT / "xcash"

# 项目 app 实际位于内层 xcash 目录；pytest 不会像 manage.py 那样自动补这段路径。
if str(APPS_DIR) not in sys.path:
    sys.path.append(str(APPS_DIR))


@pytest.fixture(autouse=True)
def _reset_system_settings_cache():
    # SystemSettings 单例走 Redis 缓存 timeout=None；TestCase 事务回滚后缓存里仍残留旧对象，
    # 会让下一个用例读到上一个测试创建的运行时开关，造成跨用例污染。
    from django.core.cache import cache  # noqa: PLC0415

    from core.models import SYSTEM_SETTINGS_CACHE_KEY  # noqa: PLC0415

    cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
    yield
    cache.delete(SYSTEM_SETTINGS_CACHE_KEY)


@pytest.fixture(autouse=True)
def _cleanup_committed_jobs(request, django_db_blocker):
    # Procrastinate 的表是 managed=False，TransactionTestCase 结束时的清库不会清空它们；
    # 这类用例真实提交的任务若留在表里，会污染后续用例对任务队列的断言。
    yield
    from django.test import TestCase  # noqa: PLC0415
    from django.test import TransactionTestCase  # noqa: PLC0415

    test_class = getattr(request, "cls", None)
    if (
        test_class is None
        or not issubclass(test_class, TransactionTestCase)
        or issubclass(test_class, TestCase)
    ):
        return
    from django.db import connection  # noqa: PLC0415

    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM procrastinate_jobs")
        cursor.execute("DELETE FROM procrastinate_periodic_defers")
        cursor.execute("DELETE FROM procrastinate_workers")


@pytest.fixture(autouse=True)
def _discard_saas_permission_refresh():
    # 权限缓存缺失或过期时，业务路径会把刷新提交到进程内后台线程，真实请求 SaaS。
    # 测试环境没有 SaaS：默认丢弃这些提交，避免后台线程在用例之间发网络请求、拖慢并
    # 干扰其它用例。验证刷新行为的用例自行 patch refresh_executor。
    from unittest.mock import Mock  # noqa: PLC0415
    from unittest.mock import patch  # noqa: PLC0415

    with patch("common.permission_check.refresh_executor", return_value=Mock()):
        yield
