from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.utils.html import format_html
from django.utils.html import format_html_join
from django.utils.translation import gettext_lazy as _
from unfold.decorators import display
from unfold.widgets import UnfoldAdminTextInputWidget
from unfold.widgets import UnfoldAdminURLInputWidget

from chains.adapters import AdapterFactory
from chains.capabilities import ChainProductCapabilityService
from chains.constants import ChainType
from chains.models import Address
from common.admin import ModelAdmin
from common.admin import ReadOnlyModelAdmin
from common.admin import StackedInline
from common.admin import TabularInline
from invoices.models import DifferRecipientAddress
from invoices.models import EpayMerchant
from projects.models import Customer
from projects.models import Project

# Register your models here.


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
            "webhook_open",
            "failed_count",
            "pre_notify",
            "fast_confirm_threshold",
            "hmac_key",
            "vault",
            "active",
        )

    def __init__(self, *args, **kwargs):
        # 从 kwargs 中提取用户
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

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

    def clean_vault(self):
        address = self.cleaned_data.get("vault")
        if not address:
            return None

        old_address = None
        if self.instance and self.instance.pk:
            old_address = (
                Project.objects.filter(pk=self.instance.pk)
                .values_list("vault", flat=True)
                .first()
            )
        if old_address:
            if old_address != address:
                raise forms.ValidationError(
                    _("VaultSlot 多签归集地址一旦设置不可修改。")
                )
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
            'class="flex items-center justify-center text-gray-400 hover:text-gray-600 '
            "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 "
            'focus-visible:outline-primary-500 dark:text-gray-500 dark:hover:text-gray-300" '
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


class DifferRecipientAddressInlineForm(forms.ModelForm):
    """差额账单收款地址 inline 表单，包含地址格式校验和跨项目占用检查。"""

    allowed_chain_types = frozenset(ChainType.values)

    class Meta:
        model = DifferRecipientAddress
        fields = ("name", "chain_type", "address")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["chain_type"].choices = [
            choice
            for choice in ChainType.choices
            if choice[0] in self.allowed_chain_types
        ]

    def clean(self):
        cleaned_data = super().clean()
        chain_type = cleaned_data.get("chain_type")
        address = cleaned_data.get("address")
        if not chain_type or not address:
            return cleaned_data

        if chain_type not in self.allowed_chain_types:
            raise ValidationError(_("当前用途不支持该地址格式"))

        adapter = AdapterFactory.get_adapter(chain_type=chain_type)
        if not adapter.validate_address(address=address):
            raise forms.ValidationError(_("地址格式错误"))

        # inline 场景下 project 由 parent 自动注入，不在 cleaned_data 里；
        # 用 instance.project_id 或 parent_instance 来做跨项目占用检查。
        project = getattr(self.instance, "project", None)
        qs = DifferRecipientAddress.objects.filter(address=address)
        if project:
            qs = qs.exclude(project=project)
        if qs.exists():
            raise ValidationError(_("地址已被其他项目占用"))

        return cleaned_data

    def clean_address(self):
        address = self.cleaned_data.get("address")
        if address and Address.objects.filter(address=address).exists():
            raise ValidationError(_("不能设置为系统内账户"))
        return address


class DifferRecipientAddressInline(TabularInline):
    """项目差额账单收款地址 inline。"""

    model = DifferRecipientAddress
    form = DifferRecipientAddressInlineForm
    extra = 0
    fields = ("name", "chain_type", "address")
    allowed_chain_types = ChainProductCapabilityService.INVOICE_RECIPIENT_CHAIN_TYPES
    verbose_name = _("差额账单收款地址")
    verbose_name_plural = _("差额账单收款地址")

    def get_formset(self, request, obj=None, **kwargs):
        base_form = self.form

        class InlineForm(base_form):
            allowed_chain_types = self.allowed_chain_types

        kwargs["form"] = InlineForm
        formset = super().get_formset(request, obj, **kwargs)
        formset.form.base_fields["chain_type"].choices = [
            choice
            for choice in ChainType.choices
            if choice[0] in self.allowed_chain_types
        ]
        return formset


class EpayMerchantInline(StackedInline):
    # EpayMerchant 与 Project 是 OneToOne，限制 max_num=1 避免在表单上误导用户可以新增多条。
    model = EpayMerchant
    extra = 0
    max_num = 1
    can_delete = False
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


@admin.register(Project)
class ProjectAdmin(ModelAdmin):
    form = ProjectForm
    inlines = (
        DifferRecipientAddressInline,
        EpayMerchantInline,
    )
    list_display = (
        "name",
        "appid",
        "display_ready_status",
        "webhook",
        "failed_count",
        "webhook_open",
        "active",
    )
    list_editable = ("active",)
    list_filter = (
        "active",
        "webhook_open",
    )
    search_fields = ("name", "appid", "webhook")

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
                "failed_count",
                "display_ready_detail",
            )
            if obj.vault:
                readonly_fields += ("vault",)
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
                    "webhook",
                ),
            },
        ),
        ("安全", {"fields": ("ip_white_list",)}),
    )
    edit_fieldsets = (
        (
            _("项目状态"),
            {
                "classes": ("wide",),
                "fields": ("display_ready_detail",),
            },
        ),
        (
            _("基本信息"),
            {
                "fields": (
                    "name",
                    "appid",
                    "fast_confirm_threshold",
                ),
            },
        ),
        (
            _("项目资金"),
            {
                "fields": ("vault",),
            },
        ),
        (
            _("安全"),
            {
                "fields": (
                    "hmac_key",
                    "ip_white_list",
                ),
            },
        ),
        (
            _("通知"),
            {
                "fields": (
                    "webhook",
                    "failed_count",
                    "webhook_open",
                ),
            },
        ),
    )

    def has_delete_permission(self, request, obj=None):
        return False  # 禁止删除

    @display(
        description=_("就绪"),
        label={
            "已就绪": "success",
            "未就绪": "danger",
        },
    )
    def display_ready_status(self, instance: Project):
        ready, _ = instance.is_ready
        return "已就绪" if ready else "未就绪"

    @display(description=_("项目状态"))
    def display_ready_detail(self, instance: Project):
        ready, errors = instance.is_ready
        if ready:
            return format_html(
                '<div class="flex items-center gap-2 py-2">'
                '<span class="inline-flex items-center justify-center w-6 h-6 rounded-full bg-green-100 dark:bg-green-900/30">'
                '<span class="material-symbols-outlined text-green-600 dark:text-green-400" style="font-size:16px">check_circle</span>'
                "</span>"
                '<span class="text-green-600 dark:text-green-400 font-semibold text-base">{}</span>'
                "</div>",
                _("所有检查项已通过，项目可正常运行"),
            )
        items = format_html_join(
            "",
            '<li class="flex items-center gap-2 py-1">'
            '<span class="material-symbols-outlined text-red-500 dark:text-red-400" style="font-size:16px">cancel</span>'
            "<span>{}</span>"
            "</li>",
            ((e,) for e in errors),
        )
        return format_html(
            '<div class="py-2">'
            '<div class="flex items-center gap-2 mb-2">'
            '<span class="inline-flex items-center justify-center w-6 h-6 rounded-full bg-red-100 dark:bg-red-900/30">'
            '<span class="material-symbols-outlined text-red-500 dark:text-red-400" style="font-size:16px">error</span>'
            "</span>"
            '<span class="text-red-600 dark:text-red-400 font-semibold text-base">{}</span>'
            "</div>"
            '<ul class="ml-8 space-y-0.5 text-sm text-red-600 dark:text-red-400">{}</ul>'
            "</div>",
            _("项目未就绪，请处理以下问题"),
            items,
        )


@admin.register(Customer)
class CustomerAdmin(ReadOnlyModelAdmin):
    list_display = ("uid", "project", "created_at")
    list_filter = ("project",)
    search_fields = ("uid",)
