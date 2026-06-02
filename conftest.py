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
