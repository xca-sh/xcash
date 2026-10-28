from django import forms
from django.contrib import admin
from django.utils.functional import lazy
from django.utils.html import format_html
from django.utils.html import format_html_join
from django.utils.safestring import mark_safe
from django.utils.text import format_lazy
from django.utils.translation import gettext_lazy as _
from unfold.contrib.filters.admin import RangeDateTimeFilter
from unfold.contrib.filters.admin import RelatedDropdownFilter
from unfold.decorators import display
from unfold.widgets import UnfoldAdminTextInputWidget
from unfold.widgets import UnfoldAdminURLInputWidget

from common import admin_display as fmt
from common.admin import ModelAdmin
from common.admin import ReadOnlyModelAdmin
from common.admin import StackedInline
from common.admin import TabularInline
from invoices.models import DifferRecipientAddress
from invoices.models import EpayMerchant
from projects.models import Customer
from projects.models import Project
from webhooks.service import delivery_url_scheme_allowed

# Register your models here.

# mark_safe 会立刻对惰性翻译字符串求值，所以它自己也必须惰性化，
# 否则 help_text 在模块导入期就被钉死成默认语言（中文），切英文不会变。
mark_safe_lazy = lazy(mark_safe, str)

# 模型里的 help_text 用 `mark_safe(_(..) + "<br>" + ..)` 拼接，字符串相加同样会在导入期
# 求值。后台表单改用 format_lazy 全程惰性拼接，保证跟随请求语言。
IP_WHITE_LIST_HELP_TEXT = mark_safe_lazy(
    format_lazy(
        "{}<br>{}<br>{}",
        _("只有符合白名单的 IP 才可以与本网关交互，支持 IP 地址或 IP 网段"),
        _("可同时设置多个，中间用英文逗号 ',' 分割"),
        _("* 代表允许所有 IP 访问"),
    )
)


class ProjectForm(forms.ModelForm):
    webhook = forms.URLField(
        label=_("通知地址"),
        required=False,
        assume_scheme="https",
        help_text=_("用于本网关发送通知到商户后端"),
        widget=UnfoldAdminURLInputWidget(),
    )

    class Meta:
        model = Project
        fields = (
            "name",
            "ip_white_list",
            "webhook",
            "fast_confirm_threshold",
            "hmac_key",
            "contract_receiving_enabled",
            "evm_vault",
            "tron_vault",
            "evm_invoice_receiving_mode",
            "tron_invoice_receiving_mode",
            "aml_screening_enabled",
            "aml_screening_threshold_usd",
            "active",
            "is_test",
        )

    def __init__(self, *args, **kwargs):
        # 从 kwargs 中提取用户
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        if "ip_white_list" in self.fields:
            self.fields["ip_white_list"].help_text = IP_WHITE_LIST_HELP_TEXT

    def clean_webhook(self):
        # 投递只接受 https，入口放行 http 会让付款后的通知直接投递失败。
        webhook = self.cleaned_data.get("webhook", "")
        if webhook and not delivery_url_scheme_allowed(webhook):
            raise forms.ValidationError(_("通知地址必须使用 https。"))
        return webhook

    def clean_ip_white_list(self):
        """
        检查设置的白名单IP 地址或网络是否合法
        :return: None
        """
        ip_white_list = self.cleaned_data.get("ip_white_list", "").strip()

        if not ip_white_list or ip_white_list == "*":
            return ip_white_list

        from common.utils.security import is_ip_or_network

        if not all(is_ip_or_network(addr) for addr in ip_white_list.split(",")):
            raise forms.ValidationError(_("IP 白名单格式错误."))

        return ip_white_list

    def clean_evm_vault(self):
        address = self.cleaned_data.get("evm_vault")
        if not address:
            return None

        old_address = None
        if self.instance and self.instance.pk:
            old_address = (
                Project.objects.filter(pk=self.instance.pk)
                .values_list("evm_vault", flat=True)
                .first()
            )
        if old_address:
            if old_address != address:
                raise forms.ValidationError(_("EVM 收款归集地址一旦设置不可修改。"))
            return old_address

        return address

    def clean_tron_vault(self):
        address = self.cleaned_data.get("tron_vault")
        if not address:
            return None

        old_address = None
        if self.instance and self.instance.pk:
            old_address = (
                Project.objects.filter(pk=self.instance.pk)
                .values_list("tron_vault", flat=True)
                .first()
            )
        if old_address:
            if old_address != address:
                raise forms.ValidationError(_("Tron 收款归集地址一旦设置不可修改。"))
            return old_address

        return address


class ProjectHmacKeyWidget(UnfoldAdminTextInputWidget):
    input_type = "password"

    class Media:
        js = ("projects/js/hmac_key_toggle.js",)

    def __init__(self, attrs=None):
        super().__init__(attrs=attrs)

        classes = self.attrs.get("class", "").split()
        if "pr-12" not in classes:
            classes.append("pr-12")
        self.attrs["class"] = " ".join(classes)

        self.attrs.setdefault("data-password-toggle-input", "true")
        self.attrs.setdefault("autocomplete", "off")

    def render(self, name, value, attrs=None, renderer=None):
        input_html = super().render(name, value, attrs=attrs, renderer=renderer)
        button_html = format_html(
            '<button type="button" '
            'class="flex items-center justify-center text-base-400 hover:text-base-600 '
            "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 "
            'focus-visible:outline-primary-500 dark:text-base-500 dark:hover:text-base-300" '
            'style="position:absolute;top:50%;right:0.5rem;transform:translateY(-50%);" '
            'data-password-toggle-button aria-label="{}" aria-pressed="false">'
            '<span class="material-symbols-outlined text-lg" data-password-toggle-icon '
            'data-hidden-label="visibility_off" data-visible-label="visibility">visibility_off</span>'
            "</button>",
            _("显示或隐藏密钥"),
        )

        return format_html(
            '<div class="max-w-2xl" data-password-toggle '
            'style="position:relative;max-width:42rem;">{}{}</div>',
            input_html,
            button_html,
        )


class EpayMerchantInline(StackedInline):
    # EpayMerchant 与 Project 是 OneToOne，限制 max_num=1 避免在表单上误导用户可以新增多条。
    model = EpayMerchant
    extra = 0
    max_num = 1
    can_delete = False
    tab = True
    verbose_name = _("EPay 配置")
    verbose_name_plural = _("EPay 配置")
    fields = (
        "pid",
        "secret_key",
        "active",
    )

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        # secret_key 是 EPay 协议签名密钥，等同 hmac_key 的敏感级别，复用项目页同款密码型 widget。
        if db_field.name == "secret_key":
            kwargs["widget"] = ProjectHmacKeyWidget()
        return super().formfield_for_dbfield(db_field, request, **kwargs)


class DifferRecipientAddressInline(TabularInline):
    model = DifferRecipientAddress
    extra = 0
    tab = True
    show_count = True
    show_title = False
    verbose_name = _("钱包直收地址")
    verbose_name_plural = _("钱包直收地址")
    fields = (
        "chain_type",
        "address",
        "active",
        "sort_order",
        "created_at",
    )
    readonly_fields = ("created_at",)


@admin.register(Project)
class ProjectAdmin(ModelAdmin):
    form = ProjectForm
    inlines = (
        DifferRecipientAddressInline,
        EpayMerchantInline,
    )
    ordering = ("-created_at",)
    list_display = (
        "display_identity",
        "display_ready_status",
        "display_environment",
        "display_webhook",
        "display_receiving_mode",
        "active",
    )
    list_editable = ("active",)
    list_filter = (
        "active",
        "is_test",
        ("created_at", RangeDateTimeFilter),
    )
    search_fields = ("name", "appid", "webhook")
    search_help_text = _("支持按项目名称、Appid 或通知地址搜索")

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "hmac_key":
            kwargs["widget"] = ProjectHmacKeyWidget()
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def get_form(self, request, obj=None, **kwargs):
        form_class = super().get_form(request, obj, **kwargs)

        class RequestForm(form_class):
            def __init__(self, *args, **kwargs):
                kwargs["user"] = request.user
                super().__init__(*args, **kwargs)

        return RequestForm

    def get_inline_instances(self, request, obj=None):
        if obj is None:
            return []
        return super().get_inline_instances(request, obj=obj)

    def get_readonly_fields(self, request, obj=None):
        if obj:  # 修改项目
            readonly_fields = (
                "appid",
                "display_ready_detail",
            )
            if obj.evm_vault:
                readonly_fields += ("evm_vault",)
            if obj.tron_vault:
                readonly_fields += ("tron_vault",)
            return readonly_fields
        # 新建项目
        return ("appid",)

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return self.add_fieldsets
        return self.edit_fieldsets

    add_fieldsets = (
        (
            _("基本信息"),
            {
                "fields": (
                    "name",
                    "is_test",
                    "webhook",
                ),
                "description": _(
                    "创建后会自动分配 Appid 与 HMAC 密钥；收款归集地址等资金配置在项目详情页补齐。"
                ),
            },
        ),
        (_("安全"), {"fields": ("ip_white_list",)}),
    )
    edit_fieldsets = (
        # 就绪状态是打开项目页最先要看的结论，放在标签页之外常驻顶部。
        (
            None,
            {
                "fields": ("display_ready_detail",),
            },
        ),
        (
            _("基本信息"),
            {
                "classes": ("tab",),
                "fields": (
                    "name",
                    "appid",
                    "is_test",
                    "active",
                    "fast_confirm_threshold",
                ),
            },
        ),
        (
            _("资金"),
            {
                "classes": ("tab",),
                "fields": (
                    "contract_receiving_enabled",
                    "evm_vault",
                    "tron_vault",
                    "evm_invoice_receiving_mode",
                    "tron_invoice_receiving_mode",
                ),
                "description": _(
                    "收款归集地址一旦设置不可修改：它参与 VaultSlot 合约地址推导，变更会让历史收款地址全部失效。"
                ),
            },
        ),
        (
            _("安全"),
            {
                "classes": ("tab",),
                "fields": (
                    "hmac_key",
                    "ip_white_list",
                ),
            },
        ),
        (
            _("通知"),
            {
                "classes": ("tab",),
                "fields": ("webhook",),
            },
        ),
        (
            _("AML 筛查"),
            {
                "classes": ("tab",),
                "fields": (
                    "aml_screening_enabled",
                    "aml_screening_threshold_usd",
                ),
                "description": _("查询数据源在系统设置中统一配置。"),
            },
        ),
    )

    def has_delete_permission(self, request, obj=None):
        return False  # 禁止删除

    @display(description=_("项目"), ordering="name", header=True)
    def display_identity(self, instance: Project):
        return (instance.name, instance.appid)

    @display(
        description=_("就绪"),
        label={
            "ready": "success",
            "not_ready": "danger",
        },
    )
    def display_ready_status(self, instance: Project):
        ready = instance.is_ready[0]
        return ("ready", _("已就绪")) if ready else ("not_ready", _("未就绪"))

    @display(
        description=_("环境"),
        ordering="is_test",
        label={
            "production": "success",
            "test": "warning",
        },
    )
    def display_environment(self, instance: Project):
        return ("test", _("测试")) if instance.is_test else ("production", _("生产"))

    @display(description=_("通知"), ordering="webhook")
    def display_webhook(self, instance: Project):
        return instance.webhook or fmt.empty()

    @display(description=_("收款模式"))
    def display_receiving_mode(self, instance: Project):
        # EVM 与 Tron 的收款模式各自独立，列表页合并成一格，避免为低频字段单开两列。
        return fmt.stacked(
            f"EVM · {instance.get_evm_invoice_receiving_mode_display()}",
            f"Tron · {instance.get_tron_invoice_receiving_mode_display()}",
        )

    @display(description=_("项目状态"))
    def display_ready_detail(self, instance: Project):
        ready, errors = instance.is_ready
        if ready:
            return format_html(
                '<div class="flex items-center gap-3 py-1">'
                '<span class="xc-icon-badge xc-icon-success">'
                '<span class="material-symbols-outlined">check_circle</span>'
                "</span>"
                '<span class="text-green-700 dark:text-green-400 font-semibold">{}</span>'
                "</div>",
                _("所有检查项已通过，项目可正常运行"),
            )
        items = format_html_join(
            "",
            '<li class="flex items-center gap-2 py-0.5">'
            '<span class="material-symbols-outlined text-red-600 dark:text-red-400" style="font-size:16px">cancel</span>'
            "<span>{}</span>"
            "</li>",
            ((e,) for e in errors),
        )
        return format_html(
            '<div class="py-1">'
            '<div class="flex items-center gap-3 mb-2">'
            '<span class="xc-icon-badge xc-icon-danger">'
            '<span class="material-symbols-outlined">error</span>'
            "</span>"
            '<span class="text-red-700 dark:text-red-400 font-semibold">{}</span>'
            "</div>"
            '<ul class="ml-12 text-sm text-red-600 dark:text-red-400">{}</ul>'
            "</div>",
            _("项目未就绪，请处理以下问题"),
            items,
        )


@admin.register(Customer)
class CustomerAdmin(ReadOnlyModelAdmin):
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("project",)
    list_display = ("uid", "project", "display_deposit_count", "created_at")
    list_filter = (("project", RelatedDropdownFilter),)
    search_fields = ("uid", "project__name")
    search_help_text = _("支持按客户 UID 或项目名称搜索")
    fields = ("uid", "project", "created_at")

    def get_queryset(self, request):
        from django.db.models import Count

        # 充值笔数是客户页最常被问到的信息，用注解一次取齐，避免逐行查询。
        return super().get_queryset(request).annotate(deposit_total=Count("deposit"))

    @display(description=_("充值笔数"), ordering="deposit_total")
    def display_deposit_count(self, instance: Customer):
        return fmt.number(instance.deposit_total)
