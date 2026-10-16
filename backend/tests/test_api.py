"""Backend API tests with a fake stage runner: no model, no subprocesses."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.app.core.config import Settings  # noqa: E402
from backend.app.main import create_app  # noqa: E402

JD = "Senior Data Engineer\n\nRequired\n" + "Experience with SQL and Python. " * 6
DOCX = "Alex_Sample_Resume_acme_0912-0000.docx"


class FakeRunner:
    """Simulates the CLI tools by writing the files each stage would produce."""

    def __init__(self, outputs_dir, fail_at=None, match_status="complete", hang_at=None, fail_code=1):
        self.outputs_dir = Path(outputs_dir)
        self.fail_at = fail_at
        self.fail_code = fail_code
        self.match_status = match_status
        self.hang_at = hang_at
        self.calls = []

    def __call__(self, args, cwd, task=None):
        script = Path(args[0]).name
        self.calls.append(script)
        if script == self.hang_at:
            if script == "match_job.py":
                run = self.outputs_dir / time.strftime("%Y%m%d-%H%M%S-abcdef")
                run.mkdir(parents=True, exist_ok=True)
                (run / "jd.txt").write_text(Path(args[args.index("--jd") + 1]).read_text(encoding="utf-8"),
                                            encoding="utf-8")
            deadline = time.time() + 5
            while not task.cancel_requested and time.time() < deadline:
                time.sleep(0.02)
            return 1, "", "killed"
        if script == self.fail_at:
            return self.fail_code, "", "boom\nsecond line"
        if script == "match_job.py":
            run = self.outputs_dir / "20260912-000000-abcdef"
            run.mkdir(parents=True, exist_ok=True)
            errors = [] if self.match_status == "complete" else [
                {"error_type": "judgment_failed", "message": "R002（error）：引用不是章节原文"}]
            (run / "run_meta.json").write_text(json.dumps({
                "run_id": run.name, "run_status": self.match_status, "scope": "full", "errors": errors,
                "started_at": "2026-09-12T00:00:00", "input": {"jd_path": "jobs/x.txt"}}), encoding="utf-8")
            if self.match_status != "failed":
                matches = [{"requirement_id": "R001", "processing_status": "ok", "verdict": "direct"}]
                if self.match_status == "partial":
                    matches.append({"requirement_id": "R002", "processing_status": "error",
                                    "verdict": "insufficient"})
                (run / "matches.json").write_text(json.dumps({"matches": matches}), encoding="utf-8")
                (run / "report.md").write_text("# 报告\n", encoding="utf-8")
            code = 0 if self.match_status == "complete" else 1
            return code, f"运行目录：{run}\n状态：{self.match_status}（范围：full）\n", \
                "[local-llm] job_readiness_summary 第 1 次尝试未通过校验"
        run = self.outputs_dir / "20260912-000000-abcdef"
        if script == "tailor_cv.py":
            (run / "cv_suggestions.md").write_text("# CV 建议\n", encoding="utf-8")
            return 1, "状态：partial", ""
        if script == "build_resume.py":
            (run / "resume_tailored.md").write_text("# ALEX SAMPLE\n", encoding="utf-8")
            return 0, "已写入", ""
        if script == "job_identity.py":
            return 0, "公司：Acme；职位：Data Engineer\n", ""
        if script == "export_docx.py":
            self.export_args = args
            (run / DOCX).write_bytes(b"PK")
            return 0, "已写入（resume）：x\n已写入（cover_letter）：y\n", ""
        if script == "interview_prep.py":
            (run / "interview_prep.md").write_text("# 面试准备\n", encoding="utf-8")
            return 0, "", ""
        return 0, "", ""


def wait_for(client, task_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["status"] not in ("queued", "running"):
            return task
        time.sleep(0.05)
    return client.get(f"/api/tasks/{task_id}").json()


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)  # registered first, so it runs after the client has shut down
        root = Path(self._tmp.name)
        self.outputs = root / "outputs"
        self.jobs_dir = root / "jobs"
        self.settings = Settings(_env_file=None, project_root=root, jobs_dir=self.jobs_dir, outputs_dir=self.outputs,
                                 resume_path=root / "r.docx", config_path=root / "c.json",
                                 frontend_dist=root / "nodist",
                                 database_url=f"sqlite:///{(root / 'state.sqlite').as_posix()}",
                                 retry_failed_generation=False)   # these tests follow one attempt; see test_worker

    def start(self, runner, online=True):
        app = create_app(self.settings, runner=runner)
        app.state.worker.pipeline.model_online = lambda: (online, "fake-model" if online else "connection refused")
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        return self.client


class FrontendTests(ApiTestCase):
    def test_index_is_revalidated_and_assets_are_not(self):
        dist = Path(self._tmp.name) / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text('<script src="/assets/index-abc.js"></script>', encoding="utf-8")
        (dist / "assets" / "index-abc.js").write_text("console.log(1)", encoding="utf-8")
        self.settings = self.settings.model_copy(update={"frontend_dist": dist})
        client = self.start(FakeRunner(self.outputs))
        index = client.get("/")
        self.assertIn("index-abc.js", index.text)
        self.assertEqual(index.headers.get("cache-control"), "no-cache")   # an update shows without Ctrl+F5
        self.assertNotIn("cache-control", client.get("/assets/index-abc.js").headers)
        self.assertEqual(client.get("/api/health").status_code, 200)


class SubmitTests(ApiTestCase):
    def test_health_and_full_job(self):
        runner = FakeRunner(self.outputs)
        client = self.start(runner)
        self.assertEqual(client.get("/api/health").json()["online"], True)
        self.assertIn("/docs", client.get("/").text)  # placeholder page until the frontend is built
        self.assertEqual(client.post("/api/jobs", json={"jd_text": "too short"}).status_code, 400)
        started = client.post("/api/jobs", json={"jd_text": JD, "jd_name": "acme", "with_interview": True})
        self.assertEqual(started.status_code, 202, started.text)
        task = wait_for(client, started.json()["id"])
        self.assertEqual(task["status"], "done", task["error"])
        self.assertEqual([s["status"] for s in task["stages"]], ["done"] * 6)
        self.assertIn("partial", task["stages"][2]["message"])
        self.assertEqual(task["stages"][4]["message"], "2 个文件 · 公司：Acme；职位：Data Engineer")
        self.assertEqual(runner.calls, ["match_job.py", "tailor_cv.py", "build_resume.py", "job_identity.py",
                                        "export_docx.py", "interview_prep.py"])
        self.assertEqual(runner.export_args[-2:], ["--label", "acme"])
        self.assertTrue(list(self.jobs_dir.glob("*-acme.txt")))
        run = task["run"]
        self.assertEqual(run["run_id"], "20260912-000000-abcdef")
        self.assertEqual([d["stem"] for d in run["docs"]],
                         ["report", "cv_suggestions", "resume_tailored", "interview_prep"])
        self.assertIn(DOCX, run["downloads"])
        self.assertEqual(run["counts"], {"direct": 1, "related": 0, "insufficient": 0, "error": 0})
        # the job row followed the task
        jobs = client.get("/api/jobs").json()
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0]["source"], jobs[0]["status"], jobs[0]["title"]), ("manual", "done", "acme"))
        self.assertEqual(jobs[0]["runs"][0]["run_id"], "20260912-000000-abcdef")
        detail = client.get(f"/api/jobs/{jobs[0]['id']}").json()
        self.assertTrue(detail["jd_text"].startswith("Senior Data Engineer"))
        self.assertEqual(detail["tasks"][0]["status"], "done")
        # documents and downloads
        doc = client.get("/api/runs/20260912-000000-abcdef/docs/resume_tailored").json()
        self.assertIn("<h1>ALEX SAMPLE</h1>", doc["html"])
        self.assertEqual(client.get(f"/api/runs/20260912-000000-abcdef/download/{DOCX}").status_code, 200)
        self.assertEqual(client.get("/api/runs/20260912-000000-abcdef/download/..%5Crun_meta.json").status_code, 404)
        self.assertEqual(client.get("/api/runs/nope/docs/report").status_code, 404)
        self.assertEqual(client.get("/api/tasks/zzz").status_code, 404)
        self.assertEqual(client.get("/api/runs").json()[0]["run_id"], "20260912-000000-abcdef")

    def test_offline_model_blocks_submission(self):
        client = self.start(FakeRunner(self.outputs), online=False)
        self.assertEqual(client.post("/api/jobs", json={"jd_text": JD}).status_code, 503)
        self.assertEqual(client.post("/api/model/start").status_code, 400)  # no launcher configured
        launched = []
        client.app.state.autopilot.launch = launched.append
        client.app.state.settings.model_launcher = Path("start-llm.bat")
        self.assertEqual(client.post("/api/model/start").json(), {"started": True, "online": False})
        self.assertEqual(launched, [Path("start-llm.bat")])

    def test_partial_match_continues_and_failed_match_stops(self):
        client = self.start(FakeRunner(self.outputs, match_status="partial"))
        task = wait_for(client, client.post("/api/jobs", json={"jd_text": JD}).json()["id"])
        self.assertEqual(task["status"], "done", task["error"])
        self.assertIn("partial：2 条要求中 1 条未处理（R002）", task["stages"][1]["message"])
        runner = FakeRunner(self.outputs, match_status="failed")
        client.app.state.worker.pipeline.runner = runner  # swap the fake stages for the failing variant
        task = wait_for(client, client.post("/api/jobs", json={"jd_text": JD}).json()["id"])
        self.assertEqual(task["status"], "failed")
        self.assertIn("引用不是章节原文", task["error"])
        self.assertNotIn("job_readiness_summary", task["error"])
        self.assertEqual(runner.calls, ["match_job.py"])
        jobs = client.get("/api/jobs").json()
        self.assertEqual(len(jobs), 1)  # the same JD pasted twice is one job
        self.assertEqual(jobs[0]["status"], "failed")

    def test_stage_failure_marks_task(self):
        client = self.start(FakeRunner(self.outputs, fail_at="build_resume.py"))
        task = wait_for(client, client.post("/api/jobs", json={"jd_text": JD}).json()["id"])
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["stages"][3]["status"], "failed")
        self.assertIn("build_resume.py 退出码 1", task["error"])
        self.assertEqual(task["stages"][4]["status"], "pending")
        self.assertEqual(task["stages"][5]["status"], "skipped")


class InputErrorTests(ApiTestCase):
    def test_exit_code_2_from_a_generation_tool_is_a_failure_not_a_partial(self):
        # tailor_cv returns 1 for "partial, files written" (accepted) and 2 for "unusable input".
        client = self.start(FakeRunner(self.outputs, fail_at="tailor_cv.py", fail_code=2))
        task = wait_for(client, client.post("/api/jobs", json={"jd_text": JD}).json()["id"])
        self.assertEqual(task["status"], "failed")
        self.assertIn("tailor_cv.py 退出码 2", task["error"])
        self.assertEqual(task["stages"][3]["status"], "pending")  # nothing ran after it


class ControlTests(ApiTestCase):
    def test_interview_later_for_a_finished_job(self):
        runner = FakeRunner(self.outputs)
        client = self.start(runner)
        task = wait_for(client, client.post("/api/jobs", json={"jd_text": JD}).json()["id"])
        self.assertEqual(task["stages"][5]["status"], "skipped")
        job_id = client.get("/api/jobs").json()[0]["id"]
        later = client.post(f"/api/jobs/{job_id}/interview")
        self.assertEqual(later.status_code, 202, later.text)
        later = wait_for(client, later.json()["id"])
        self.assertEqual(later["status"], "done", later["error"])
        self.assertEqual(later["kind"], "interview")
        self.assertEqual(runner.calls[-1], "interview_prep.py")
        self.assertEqual(client.get("/api/jobs").json()[0]["status"], "done")  # unchanged by interview prep

    def test_cancel_removes_what_the_task_created(self):
        older = self.outputs / "20200101-000000-aaaaaa"  # a finished run of the very same JD
        older.mkdir(parents=True)
        runner = FakeRunner(self.outputs, hang_at="match_job.py")
        client = self.start(runner)
        task_id = client.post("/api/jobs", json={"jd_text": JD, "jd_name": "acme"}).json()["id"]
        deadline = time.time() + 5
        while "match_job.py" not in runner.calls and time.time() < deadline:
            time.sleep(0.02)
        jd_file = next(self.jobs_dir.glob("*-acme.txt"))
        (older / "jd.txt").write_text(jd_file.read_text(encoding="utf-8"), encoding="utf-8")
        self.assertEqual(client.post(f"/api/tasks/{task_id}/cancel").status_code, 202)
        task = wait_for(client, task_id)
        self.assertEqual(task["status"], "cancelled")
        self.assertIn("已删除未完成的内容", task["error"])
        self.assertEqual([p.name for p in self.outputs.iterdir()], [older.name])
        self.assertEqual(list(self.jobs_dir.glob("*.txt")), [])
        self.assertEqual(client.post(f"/api/tasks/{task_id}/cancel").status_code, 409)
        self.assertEqual(client.get("/api/jobs").json()[0]["status"], "cancelled")
        # the slot is free again: the same JD can be submitted once more, and cancelled once more
        again = client.post("/api/jobs", json={"jd_text": JD, "jd_name": "acme"})
        self.assertEqual(again.status_code, 202, again.text)
        deadline = time.time() + 5
        while runner.calls.count("match_job.py") < 2 and time.time() < deadline:
            time.sleep(0.02)
        client.post(f"/api/tasks/{again.json()['id']}/cancel")
        self.assertEqual(wait_for(client, again.json()["id"])["status"], "cancelled")
        self.assertEqual(len(client.get("/api/jobs").json()), 1)  # same JD, same job row

    def test_decision_reruns_a_job_from_its_jd_file(self):
        runner = FakeRunner(self.outputs)
        client = self.start(runner)
        task = wait_for(client, client.post("/api/jobs", json={"jd_text": JD, "jd_name": "acme"}).json()["id"])
        job_id = client.get("/api/jobs").json()[0]["id"]
        self.assertEqual(client.post(f"/api/jobs/{job_id}/decision", json={"action": "skip"}).status_code, 202)
        self.assertEqual(client.get(f"/api/jobs/{job_id}").json()["status"], "screened_out")
        rerun = client.post(f"/api/jobs/{job_id}/decision", json={"action": "rerun"})
        self.assertEqual(rerun.status_code, 202, rerun.text)
        rerun = wait_for(client, rerun.json()["id"])
        self.assertEqual(rerun["status"], "done", rerun["error"])
        self.assertEqual(rerun["stages"][0]["status"], "skipped")  # the JD was already on disk
        self.assertEqual(client.get(f"/api/jobs/{job_id}").json()["status"], "done")
        self.assertEqual(len(list(self.jobs_dir.glob("*.txt"))), 1)  # no second JD file


if __name__ == "__main__":
    unittest.main()
