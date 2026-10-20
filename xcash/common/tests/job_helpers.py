"""测试辅助：查看任务队列（procrastinate_jobs）里已入队的任务。"""

from procrastinate.contrib.django.models import ProcrastinateJob


def queued_jobs(task_name: str) -> list[ProcrastinateJob]:
    return list(ProcrastinateJob.objects.filter(task_name=task_name).order_by("id"))


def queued_args(task_name: str) -> list[dict]:
    """按入队顺序返回该任务全部已入队任务的参数。"""
    return [job.args for job in queued_jobs(task_name)]
