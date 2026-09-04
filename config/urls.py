# ruff: noqa
from django.conf import settings
from django.contrib import admin
from django.urls import include
from django.urls import path
from django.views.generic import RedirectView

from core.dashboard import operational_inspection_view
from core.health import health_view
from core.health import scanning_health_view
from invoices.epay.views import EpaySubmitView
from invoices.views import payment_view


def build_admin_urlpatterns():
    admin_urlpatterns = []
    if settings.ADMIN_PATH:
        admin_urlpatterns.append(
            path(
                settings.ADMIN_PATH,
                RedirectView.as_view(pattern_name="admin:index", permanent=False),
                name="admin-path-redirect",
            )
        )

    admin_urlpatterns.extend(
        [
            # 自定义登录路由跟随 ADMIN_PATH，且需要先于 admin.site.urls 注册。
            path(settings.ADMIN_ROUTE_PREFIX, include("users.urls")),
            path(
                f"{settings.ADMIN_ROUTE_PREFIX}operations/inspection",
                # 为“异常巡检”提供独立后台页，避免继续复用 admin 首页。
                admin.site.admin_view(operational_inspection_view),
                name="operational-inspection",
            ),
            # 后台认证路由：必须先于 admin.site.urls 注册
            path(settings.ADMIN_ROUTE_PREFIX, admin.site.urls),
        ]
    )
    return admin_urlpatterns


urlpatterns = [
    # 容器健康探测。必须注册在 build_admin_urlpatterns() 之前：未设置 ADMIN_PATH 时
    # admin 挂在站点根路径且是 catch-all，后注册的 /health 会被它吞掉。
    path("health", health_view, name="health"),
    # 扫描存活探测，供外部 uptime 监控拉取。有意不参与 django 容器的 healthcheck：
    # 扫描停摆时 django 本身是健康的，混入会导致误杀。
    path("health/scanning", scanning_health_view, name="health-scanning"),
    path("v1/", include("config.api_v1")),
    path("epay/submit.php", EpaySubmitView.as_view(), name="epay-submit"),
    # 支付前端 SPA：返回 index.html，由 React 根据 sys_no 渲染支付页
    path("pay/<str:sys_no>", payment_view, name="payment-invoice"),
    path("i18n/", include("django.conf.urls.i18n")),
]

if settings.IS_SAAS:
    urlpatterns += [path("saas/v1/", include("saas_api.urls"))]

if settings.DEBUG and "stress" in settings.INSTALLED_APPS:
    # stress webhook 必须优先于 admin catch-all 注册，否则 /stress/webhook/ 会被后台路由吞掉。
    urlpatterns += [path("stress/", include("stress.urls"))]

urlpatterns += build_admin_urlpatterns()

if settings.DEBUG:
    if "debug_toolbar" in settings.INSTALLED_APPS:
        import debug_toolbar

        urlpatterns = [path("__debug__/", include(debug_toolbar.urls))] + urlpatterns
