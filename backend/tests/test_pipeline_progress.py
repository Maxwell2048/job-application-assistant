import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.core.config import Settings
from backend.app.services.pipeline import Pipeline, Task, run_command


class ProgressTests(unittest.TestCase):
    def test_streams_both_pipes_before_child_finishes_and_preserves_output(self):
        with tempfile.TemporaryDirectory() as root:
            script = Path(root) / "stage.py"
            release = Path(root) / "release"
            script.write_text("import sys,time\nfrom pathlib import Path\n"
                              "print('[progress] 匹配证据 1/2',flush=True)\n"
                              "print('diagnostic',file=sys.stderr,flush=True)\n"
                              f"release=Path({str(release)!r})\n"
                              "deadline=time.monotonic()+5\n"
                              "while not release.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
                              "sys.exit(0 if release.exists() else 9)\n",encoding="utf-8")
            task = Task()
            received = []
            def progress(line):
                received.append(line)
                if "匹配证据" in line:
                    self.assertIsNone(task.process.poll())
                    release.touch()
            task.on_progress = progress
            code, stdout, stderr = run_command([str(script)], root, task)
            self.assertEqual(code, 0)
            self.assertIn("匹配证据", stdout)
            self.assertIn("diagnostic", stderr)
            self.assertIsNone(task.process)

    def test_pipeline_publishes_real_progress_and_elapsed_time(self):
        task = Task()
        updates = []
        pipeline = Pipeline(Settings(_env_file=None), on_change=lambda task: updates.append(dict(task.stage("match"))))
        def work():
            with patch("backend.app.services.pipeline.time.time", return_value=100):
                task.on_progress("[progress] 匹配证据 3/12 · 已完成 2 条")
            return "complete"
        with patch("backend.app.services.pipeline.time.time", return_value=10):
            pipeline._step(task, "match", work)
        self.assertTrue(any(s["status"] == "running" and "3/12" in s["message"] and s["seconds"] == 90 for s in updates))
        self.assertEqual(task.stage("match")["message"], "complete")
        self.assertIsNone(task.on_progress)
