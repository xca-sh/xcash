"""chain-runner 进程入口：按链常驻扫描与收发主动交易，直到收到 SIGTERM/SIGINT。"""

import signal

from django.core.management.base import BaseCommand

from chains.runner import ChainRunner


class Command(BaseCommand):
    help = "按链常驻运行扫描、终局轮询与派发循环（chain-runner）"

    def handle(self, *args, **options):
        runner = ChainRunner()

        def request_stop(signum, frame):  # noqa: ARG001
            # 只置停止标志：各链跑完当前一步后退出，进度均已按单元提交。
            runner.stop()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        runner.run_forever()
