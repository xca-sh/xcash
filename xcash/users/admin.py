from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _
from unfold.forms import AdminPasswordChangeForm

from common.admin import ModelAdmin
from projects.models import Project

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
            None,
            {
                "fields": (
                    "username",
                    "password",
                )
            },
        ),
        (
            _("权限"),
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                ),
            },
        ),
        (_("重要日期"), {"fields": ("last_login", "date_joined")}),
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
    list_display = [
        "username",
        "is_superuser",
        "is_staff",
        "is_active",
    ]
    search_fields = ["username"]
    ordering = ("id",)


class ProjectListFilter(admin.SimpleListFilter):
    title = _("项目")
    parameter_name = "project"

    def lookups(self, request, model_admin):
        return tuple(
            (project.pk, project.name) for project in Project.objects.order_by("name")
        )

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(project_id=self.value())
        return queryset
