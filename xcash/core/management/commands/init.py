"""初始化 Xcash 系统。"""

from django.core.management.base import BaseCommand

from core.default_data import ensure_default_reference_data


class Command(BaseCommand):
    help = "初始化 Xcash 系统：基础数据等"

    def handle(self, *args, **options):
        try:
            ensure_default_reference_data(stdout=self.stdout)

            self.stdout.write(self.style.SUCCESS("所有初始化任务完成"))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"初始化失败: {e}"))
