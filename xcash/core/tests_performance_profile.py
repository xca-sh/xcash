import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config.performance import get_int
from config.performance import profile_name


class PerformanceProfileTests(SimpleTestCase):
    def test_default_profile_is_low(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(profile_name(), "low")

    def test_explicit_env_overrides_profile_value(self):
        with patch.dict(
            os.environ,
            {"PERFORMANCE": "medium", "CELERY_WORKER_CONCURRENCY": "9"},
            clear=True,
        ):
            self.assertEqual(
                get_int("CELERY_WORKER_CONCURRENCY", "celery_worker_concurrency"), 9
            )

    def test_invalid_profile_fails_fast(self):
        with (
            patch.dict(os.environ, {"PERFORMANCE": "tiny"}, clear=True),
            self.assertRaises(ImproperlyConfigured),
        ):
            profile_name()

    def test_shell_env_outputs_profile_values(self):
        env = {
            **os.environ,
            "PERFORMANCE": "high",
            "GUNICORN_WORKERS": "9",
            "GUNICORN_THREADS": "5",
            "DJANGO_SETTINGS_MODULE": "config.settings.test",
        }
        performance_script = (
            Path(__file__).resolve().parents[2] / "config" / "performance.py"
        )
        completed = subprocess.run(
            [sys.executable, str(performance_script), "shell-env", "web"],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertIn("export GUNICORN_WORKERS=9", completed.stdout)
        self.assertIn("export GUNICORN_THREADS=5", completed.stdout)

    def test_celery_beat_schedule_overridable(self):
        # beat 周期可由 CELERY_*_SCHEDULE_SECONDS 环境变量覆盖。
        env = {
            **os.environ,
            "CELERY_FALLBACK_PROCESS_TRANSFER_SCHEDULE_SECONDS": "13",
            "DJANGO_SETTINGS_MODULE": "config.settings.test",
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from config.celery import app;"
                    "entry = app.conf.beat_schedule['fallback_process_transfer'];"
                    "print(entry['task']);"
                    "print(entry['schedule'])"
                ),
            ],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(
            completed.stdout.splitlines(),
            ["chains.tasks.fallback_process_transfer", "13"],
        )
