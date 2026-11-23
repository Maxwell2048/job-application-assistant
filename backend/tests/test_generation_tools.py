"""Coverage gate, partial redo, application answers, call log and restart notice."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.tests.test_api import ApiTestCase, FakeRunner, JD, wait_for  # noqa: E402

RUN = "20260912-000000-abcdef"


class GatedRunner(FakeRunner):
    """match_job writes conditions of which `gaps` of 6 lack evidence; the answer
    tool writes an answer file."""

    def __init__(self, outputs_dir, gaps=5, **kwargs):
        super().__init__(outputs_dir, **kwargs)
        self.gaps = gaps
        self.args = []

    def __call__(self, args, cwd, task=None):
        self.args.append(args)
        result = super().__call__(args, cwd, task)
        script = Path(args[0]).name
        run = self.outputs_dir / RUN
        if script == "match_job.py":
            requirements = [{"requirement_id": f"R{i}", "category": "technical_skill", "importance": "required"}
                            for i in range(6)]
            matches = [{"requirement_id": f"R{i}", "processing_status": "ok",
                        "verdict": "insufficient" if i < self.gaps else "direct"} for i in range(6)]
            (run / "requirements.json").write_text(json.dumps({"requirements": requirements}), encoding="utf-8")
            (run / "matches.json").write_text(json.dumps({"matches": matches}), encoding="utf-8")
        if script == "tailor_cv.py":
            (run / "cv_suggestions.json").write_text(json.dumps({"projects": []}), encoding="utf-8")
        if script == "build_resume.py":
            (run / "resume_tailored.json").write_text(json.dumps({"sections": []}), encoding="utf-8")
        if script == "application_answer.py":
            request = json.loads(Path(args[args.index("--request") + 1]).read_text(encoding="utf-8"))
            (run / "application_answers.json").write_text(json.dumps([{
                "id": request["id"], "question": request["question"], "status": "complete", "words": 3,
                "answer": [{"text": "I built things.", "basis": []}], "to_confirm": ["无犯罪记录证明"]}]),
                encoding="utf-8")
            return 0, "回答：complete（3 词）", ""
        if script == "quality_report.py":
            (run / "quality.json").write_text(json.dumps({"level": "warn", "issues": 1, "checks": []}),
                                              encoding="utf-8")
            return 1, f"{RUN} 质量：1 项提醒", ""
        return result


class CoverageGateTests(ApiTestCase):
    def jd_file(self):
        path = self.jobs_dir / "x.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(JD, encoding="utf-8")
        return path

    def test_automatic_queue_stops_at_low_coverage_and_page_can_continue(self):
        runner = GatedRunner(self.outputs, gaps=5)
        client = self.start(runner)
        worker = client.app.state.worker
        task = worker.submit_full(jd_path=self.jd_file(), jd_name="acme", priority=100)
        task = wait_for(client, task.id)
        self.assertEqual(task["status"], "low_coverage")
        self.assertIn("5 项证据不足", task["error"])
        self.assertEqual(runner.calls, ["match_job.py"])
        self.assertEqual([s["status"] for s in task["stages"][2:5]], ["skipped"] * 3)
        # continue from the job page: tailor -> build -> docx, without matching again
        redo = client.post(f"/api/runs/{RUN}/redo", json={"step": "tailor"})
        self.assertEqual(redo.status_code, 202, redo.text)
        done = wait_for(client, redo.json()["id"])
        self.assertEqual(done["status"], "done", done["error"])
        self.assertEqual(runner.calls[1:], ["tailor_cv.py", "build_resume.py", "job_identity.py", "export_docx.py",
                                            "quality_report.py"])
        self.assertIn("质量：1 项提醒", done["stages"][4]["message"])
        self.assertEqual(client.get(f"/api/runs/{RUN}").json()["quality"]["level"], "warn")

    def test_page_decision_and_good_coverage_pass_the_gate(self):
        runner = GatedRunner(self.outputs, gaps=2)
        client = self.start(runner)
        task = wait_for(client, client.app.state.worker.submit_full(jd_path=self.jd_file(), priority=100).id)
        self.assertEqual(task["status"], "done", task["error"])
        runner = GatedRunner(self.outputs, gaps=6)
        client.app.state.worker.pipeline.runner = runner
        forced = client.app.state.worker.submit_full(jd_path=self.jd_file(), force=True)
        self.assertEqual(wait_for(client, forced.id)["status"], "done")


class RedoAndAnswerTests(ApiTestCase):
    def full_run(self, client):
        started = client.post("/api/jobs", json={"jd_text": JD, "jd_name": "acme"})
        return wait_for(client, started.json()["id"])

    def test_closing_redo_rewrites_letter_and_skips_matching(self):
        runner = GatedRunner(self.outputs, gaps=6)
        client = self.start(runner)
        self.assertEqual(self.full_run(client)["status"], "done")   # a pasted JD passes the gate
        runner.calls.clear()
        task = wait_for(client, client.post(f"/api/runs/{RUN}/redo", json={"step": "closing"}).json()["id"])
        self.assertEqual(task["status"], "done", task["error"])
        self.assertEqual(runner.calls[0], "tailor_cv.py")
        self.assertIn("--redo-closing", runner.args[-5])
        self.assertEqual([s["status"] for s in task["stages"]][:2], ["skipped", "skipped"])
        export = wait_for(client, client.post(f"/api/runs/{RUN}/redo", json={"step": "export"}).json()["id"])
        self.assertEqual(runner.calls[-3:], ["job_identity.py", "export_docx.py", "quality_report.py"])
        self.assertEqual(export["status"], "done")
        self.assertEqual(client.post(f"/api/runs/{RUN}/redo", json={"step": "everything"}).status_code, 422)

    def test_health_reports_files_needing_a_restart(self):
        client = self.start(GatedRunner(self.outputs))
        self.assertEqual(client.get("/api/health").json()["restart_needed"], [])

    def test_redo_needs_the_files_of_the_earlier_step(self):
        client = self.start(GatedRunner(self.outputs))
        (self.outputs / RUN).mkdir(parents=True)
        self.assertEqual(client.post(f"/api/runs/{RUN}/redo", json={"step": "assemble"}).status_code, 409)

    def test_answer_is_queued_and_listed_with_facts_to_confirm(self):
        runner = GatedRunner(self.outputs, gaps=0)
        client = self.start(runner)
        self.full_run(client)
        response = client.post(f"/api/runs/{RUN}/answers",
                               json={"question": "What relevant skills would you bring?", "max_words": 200})
        self.assertEqual(response.status_code, 202, response.text)
        task = wait_for(client, response.json()["id"])
        self.assertEqual((task["status"], task["kind"]), ("done", "answer"))
        answers = client.get(f"/api/runs/{RUN}").json()["answers"]
        self.assertEqual(answers[0]["to_confirm"], ["无犯罪记录证明"])
        self.assertFalse(list((self.outputs / RUN).glob("answer_request-*.json")))   # the request file is removed
        self.assertEqual(client.post(f"/api/runs/{RUN}/answers", json={"question": "x"}).status_code, 422)


class CallLogAndRestartTests(unittest.TestCase):
    def test_model_calls_are_logged_when_the_variable_is_set(self):
        from local_llm import CALL_LOG_ENV, log_call
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calls.jsonl"
            os.environ[CALL_LOG_ENV] = str(path)
            try:
                log_call(task="cv_summary", attempts=2, seconds=1.5, outcome="ok", failures=[{"type": "validation"}])
            finally:
                del os.environ[CALL_LOG_ENV]
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual((record["task"], record["attempts"]), ("cv_summary", 2))
            log_call(task="nothing")   # no variable: nothing written, no error

    def test_changed_loaded_module_is_reported(self):
        from backend.app.core import code_version
        with tempfile.TemporaryDirectory() as tmp:
            module = Path(tmp) / "probe_module_xyz.py"
            module.write_text("X = 1\n", encoding="utf-8")
            sys.path.insert(0, tmp)
            try:
                __import__("probe_module_xyz")
                self.assertEqual(code_version.changed_files(tmp), [])
                os.utime(module, (module.stat().st_atime, module.stat().st_mtime + 10))
                self.assertEqual(code_version.changed_files(tmp), ["probe_module_xyz.py"])
            finally:
                sys.path.remove(tmp)
                sys.modules.pop("probe_module_xyz", None)


if __name__ == "__main__":
    unittest.main()
