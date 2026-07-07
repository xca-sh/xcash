from aml.models import RiskAssessment
from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from common.admin import ReadOnlyModelAdmin


@admin.register(RiskAssessment)
class RiskAssessmentAdmin(ReadOnlyModelAdmin):
    list_display = (
        "id",
        "target_type",
        "source",
        "status",
        "risk_level",
        "risk_score",
        "address",
        "tx_hash",
        "checked_at",
        "created_at",
    )
    list_filter = ("source", "status", "risk_level", "target_type")
    search_fields = (
        "address",
        "tx_hash",
        "invoice__sys_no",
        "deposit__sys_no",
    )
    readonly_fields = (
        "source",
        "status",
        "target_type",
        "invoice",
        "deposit",
        "address",
        "tx_hash",
        "risk_level",
        "risk_score",
        "raw_response",
        "error_message",
        "checked_at",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (
            _("目标"),
            {
                "fields": (
                    "target_type",
                    "invoice",
                    "deposit",
                    "address",
                    "tx_hash",
                )
            },
        ),
        (
            _("风险结果"),
            {
                "fields": (
                    "source",
                    "status",
                    "risk_level",
                    "risk_score",
                    "raw_response",
                    "error_message",
                )
            },
        ),
        (_("时间"), {"fields": ("checked_at", "created_at", "updated_at")}),
    )
