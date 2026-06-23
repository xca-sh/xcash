from __future__ import annotations

from django.core.management.base import BaseCommand

from core.default_data import ensure_default_reference_data


class Command(BaseCommand):
    help = "幂等补齐系统默认主数据"

    def add_arguments(self, parser):
        parser.add_argument(
            "--database",
            default="default",
            help="指定要补齐主数据的数据库别名",
        )

    def handle(self, *args, **options):
        ensure_default_reference_data(
            using=options["database"],
            stdout=self.stdout,
        )
