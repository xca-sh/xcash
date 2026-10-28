from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group
from django.utils.translation import gettext_lazy as _
from rest_framework.authtoken.admin import TokenAdmin as BaseTokenAdmin
from rest_framework.authtoken.models import TokenProxy
from unfold.contrib.filters.admin import RangeDateTimeFilter
from unfold.decorators import display
from unfold.forms import AdminPasswordChangeForm

from common import admin_display as fmt
from common.admin import ModelAdmin

from .forms import UserAdminChangeForm
from .forms import UserAdminCreationForm
from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin):
    # 用户模型已切换为 username 登录，这里同步移除已失效的 edition/balance/account 配置。
    form = UserAdminChangeForm
    add_form = UserAdminCreationForm
    change_password_form = AdminPasswordChangeForm
    fieldsets = (
        (
            _("账号"),
            {
                "classes": ("tab",),
                "fields": (
                    "username",
                    "password",
                ),
            },
        ),
        (
            _("权限"),
            {
                "classes": ("tab",),
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                ),
                "description": _(
                    "超级管理员拥有全部后台能力，包括扫描游标启停、归集重排等资金治理操作；"
                    "只做审计查看的账号请只给 staff + 只读权限组。"
                ),
            },
        ),
        (
            _("重要日期"),
            {
                "classes": ("tab",),
                "fields": ("last_login", "date_joined"),
            },
        ),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("username", "password1", "password2"),
            },
        ),
    )
    filter_horizontal = ("groups", "user_permissions")
    list_display = (
        "username",
        "display_role",
        "is_active",
        "last_login",
        "date_joined",
    )
    list_filter = (
        "is_superuser",
        "is_staff",
        "is_active",
        ("date_joined", RangeDateTimeFilter),
    )
    search_fields = ["username"]
    search_help_text = _("支持按用户名搜索")
    ordering = ("-id",)
    readonly_fields = ("last_login", "date_joined")

    @display(
        description=_("角色"),
        ordering="is_superuser",
        label={
            "superuser": "danger",
            "staff": "info",
            "none": "",
        },
    )
    def display_role(self, instance: User):
        # 后台权限边界是安全关注点，列表页直接给出结论，不让人去逐个勾选框比对。
        if instance.is_superuser:
            return ("superuser", _("超级管理员"))
        if instance.is_staff:
            return ("staff", _("后台用户"))
        return ("none", _("无后台权限"))


# auth.Group 与 authtoken.TokenProxy 由第三方包用 django 原生 ModelAdmin 注册，
# 在 unfold 后台里拿不到统一的筛选、搜索与表单样式，这里替换成项目基类重新注册。
# 两者都保留上游 Admin 类作为父类，避免丢掉 TokenAdmin 的 User↔Token 主键映射逻辑。
admin.site.unregister(Group)
admin.site.unregister(TokenProxy)


@admin.register(Group)
class GroupAdmin(BaseGroupAdmin, ModelAdmin):
    list_display = ("name", "display_permission_count")
    ordering = ("-id",)
    search_fields = ("name",)
    search_help_text = _("支持按权限组名称搜索")
    filter_horizontal = ("permissions",)

    def get_queryset(self, request):
        from django.db.models import Count

        return (
            super()
            .get_queryset(request)
            .annotate(permission_total=Count("permissions"))
        )

    @display(description=_("权限数"), ordering="permission_total")
    def display_permission_count(self, obj: Group):
        return fmt.number(obj.permission_total)


@admin.register(TokenProxy)
class TokenAdmin(BaseTokenAdmin, ModelAdmin):
    list_display = ("display_key", "user", "created")
    ordering = ("-created",)
    search_help_text = _("支持按用户名搜索")

    @display(description=_("Token"), ordering="key")
    def display_key(self, obj: TokenProxy):
        # Token 明文等同密码，列表页只给首尾片段用于核对，完整值不铺在页面上。
        return fmt.truncated(obj.key, title=_("完整 Token 不在列表页展示"))
