import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config.performance import profile_name
from config.performance import shell_env


class PerformanceProfileTests(SimpleTestCase):
    def test_default_profile_is_low(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(profile_name(), "low")

    def test_explicit_env_overrides_worker_default(self):
        with patch.dict(
            os.environ,
            {"PERFORMANCE": "medium", "WORKER_CONCURRENCY": "9"},
            clear=True,
        ):
            self.assertEqual(shell_env("worker")["WORKER_CONCURRENCY"], 9)

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
