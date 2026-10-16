"""Long unattended batches: queue order, waiting for the model, one retry,
recovery after a restart, and the two search modes. No network, no
subprocesses: the generation itself is replaced by a stub."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.app.db.models import Base, Job, Screening, Task as TaskRow  # noqa: E402
from backend.app.db.session import make_engine, make_session_factory  # noqa: E402
from backend.app.services import worker as worker_module  # noqa: E402
from backend.app.services import screen  # noqa: E402
from backend.app.services.autopilot import Autopilot  # noqa: E402
from backend.app.services.sources import seek  # noqa: E402
from backend.app.services.worker import PRIORITY_SCREENED, Worker  # noqa: E402
from backend.tests.test_autopilot import FIT_PLAIN, FakeLLM, fake_collector_run, settings_for, ROWS  # noqa: E402


def until(condition, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = settings_for(self.root)
        self.engine = make_engine(self.settings.database_url)
        Base.metadata.create_all(self.engine)   # independent of how the app creates or migrates the schema
        self.sessions = make_session_factory(self.engine)
        self.launched = []
        self.worker = Worker(self.settings, self.sessions, launch=self.launched.append)
        self.worker.pipeline.model_online = lambda: (True, "fake")
        self.ran = []
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.stop_worker)

    def stop_worker(self):
        self.worker.stop()
        if self.worker._thread is not None:
            self.worker._thread.join(5)

    def status_of(self, job_id):
        with self.sessions() as session:
            return session.get(Job, job_id).status

    def stub_generation(self, fail_first=()):
        """Replace the stages: record the order, fail the first attempt of the named jobs."""
        def run_full(task, jd_text=None):
            self.ran.append((task.jd_name, task.attempt))
            task.status = "failed" if task.jd_name in fail_first and task.attempt == 1 else "done"
            task.error = "校验未通过" if task.status == "failed" else None
            self.worker._persist(task)
            return task
        self.worker.pipeline.run_full = run_full

    def add_job(self, title, status="queued", jd=True, source="seek", fit=None):
        path = self.root / "jobs" / f"{title}.txt"
        if jd:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("JD " + title, encoding="utf-8")
        with self.sessions() as session:
            job = Job(source=source, external_id=title, title=title, status=status, jd_path=str(path) if jd else "",
                      jd_sha256="x" if jd else "")
            session.add(job)
            session.flush()
            if fit is not None:
                session.add(Screening(job_id=job.id, rule_result={"fit": fit}, decision="run"))
            session.commit()
            return job.id


class QueueTests(WorkerCase):
    def test_priority_order_with_pasted_jds_first(self):
        self.stub_generation()
        self.worker.submit_full(jd_name="mid", jd_path=self.root / "x", priority=PRIORITY_SCREENED + 20)
        self.worker.submit_full(jd_name="junior", jd_path=self.root / "x", priority=PRIORITY_SCREENED + 1)
        self.worker.submit_full(jd_name="pasted", jd_text="JD", priority=0)
        self.worker.submit_full(jd_name="junior-2", jd_path=self.root / "x", priority=PRIORITY_SCREENED + 1)
        self.assertEqual(len(self.worker.queue_status()["waiting"]), 4)
        self.worker.start()
        self.assertTrue(until(lambda: len(self.ran) == 4))
        self.assertEqual([name for name, _ in self.ran], ["pasted", "junior", "junior-2", "mid"])

    def test_a_failed_generation_is_tried_once_more(self):
        self.stub_generation(fail_first={"flaky"})
        job_id = self.add_job("flaky")
        self.worker.submit_full(jd_name="flaky", job_id=job_id, jd_path=self.root / "jobs" / "flaky.txt")
        self.worker.start()
        self.assertTrue(until(lambda: len(self.ran) == 2))
        self.assertEqual(self.ran, [("flaky", 1), ("flaky", 2)])
        self.assertTrue(until(lambda: self.status_of(job_id) == "done"))
        # a second failure is final
        self.ran.clear()
        self.worker.pipeline.run_full = lambda task, jd_text=None: (
            self.ran.append(task.attempt), setattr(task, "status", "failed"), self.worker._persist(task))
        self.worker.submit_full(jd_name="flaky", job_id=job_id, jd_path=self.root / "jobs" / "flaky.txt")
        self.assertTrue(until(lambda: len(self.ran) == 2))
        time.sleep(0.2)
        self.assertEqual(self.ran, [1, 2])

    def test_the_queue_waits_for_the_model_and_starts_it(self):
        self.stub_generation()
        answers = iter([False, False, False])
        self.worker.pipeline.model_online = lambda: (next(answers, True), "connection refused")
        self.worker.settings = self.settings.model_copy(update={"model_launcher": self.root / "start-llm.bat"})
        with patch.object(worker_module, "MODEL_POLL_SECONDS", 0.05):
            self.worker.submit_full(jd_name="waits", jd_path=self.root / "x")
            self.worker.start()
            self.assertTrue(until(lambda: self.worker.queue_status()["paused"] != ""))
            self.assertIn("模型离线", self.worker.queue_status()["paused"])
            self.assertTrue(until(lambda: self.ran == [("waits", 1)]))
        self.assertEqual(self.launched, [self.root / "start-llm.bat"])      # started once, not every poll
        self.assertEqual(self.worker.queue_status()["paused"], "")

    def test_stopping_while_paused_leaves_the_job_queued(self):
        self.stub_generation()
        self.worker.pipeline.model_online = lambda: (False, "down")
        job_id = self.add_job("later")
        with patch.object(worker_module, "MODEL_POLL_SECONDS", 0.05):
            self.worker.submit_full(jd_name="later", job_id=job_id, jd_path=self.root / "jobs" / "later.txt")
            self.worker.start()
            self.assertTrue(until(lambda: self.worker.paused != ""))
            self.worker.stop()
            self.worker._thread.join(2)
        self.assertEqual(self.ran, [])
        self.assertEqual(self.status_of(job_id), "queued")


class RecoveryTests(WorkerCase):
    def test_restart_puts_unfinished_jobs_back_best_first(self):
        self.stub_generation()
        junior = {"level": "junior", "role_family": "software_dev", "flags": [], "required_stack": []}
        mid = {"level": "mid", "role_family": "it_support", "flags": ["a"], "required_stack": []}
        was_running = self.add_job("was-running", status="processing", fit=mid)
        self.add_job("was-queued", status="queued", fit=junior)
        pasted = self.add_job("pasted", status="queued", source="manual")
        lost = self.add_job("lost", status="queued", jd=False)
        done = self.add_job("done", status="done")
        with self.sessions() as session:
            session.add_all([TaskRow(id="t-running", kind="full", job_id=was_running, status="running"),
                             TaskRow(id="t-interview", kind="interview", job_id=done, status="queued"),
                             TaskRow(id="t-tick", kind="tick", status="done")])
            session.commit()
        self.assertEqual(self.worker.recover(), 3)
        with self.sessions() as session:
            self.assertEqual(session.get(TaskRow, "t-running").status, "failed")
            self.assertIn("重新排队", session.get(TaskRow, "t-running").error)
            self.assertEqual(session.get(TaskRow, "t-interview").status, "failed")
            self.assertEqual(session.get(TaskRow, "t-tick").status, "done")
            self.assertEqual(session.get(Job, lost).status, "failed")
            self.assertEqual(session.get(Job, done).status, "done")
            self.assertEqual(session.get(Job, was_running).status, "queued")
        self.worker.start()
        self.assertTrue(until(lambda: len(self.ran) == 3))
        self.assertEqual([name for name, _ in self.ran], ["pasted", "was-queued", "was-running"])
        self.assertTrue(until(lambda: self.status_of(pasted) == "done"))

    def test_fit_rank_orders_levels_then_kind_of_work(self):
        rank = lambda **fit: screen.fit_rank({"role_family": "software_dev", "flags": [], **fit}, self.settings)  # noqa
        self.assertLess(rank(level="junior"), rank(level="unclear"))
        self.assertLess(rank(level="unclear"), rank(level="mid"))
        self.assertLess(rank(level="junior"), rank(level="junior", role_family="it_support"))
        self.assertLess(rank(level="junior", required_stack=["Python"], missing_stack=[]),
                        rank(level="junior", required_stack=["C#"], missing_stack=["C#"]))
        self.assertEqual(screen.fit_rank(None, self.settings), 15.0)


class ModeTests(WorkerCase):
    def autopilot(self, runner):
        llm = FakeLLM(lambda jd: {"verdict": "unspecified", "quote": "", "reason": "x"})
        return Autopilot(self.settings, self.sessions, self.worker, launch=lambda path: None, seek_runner=runner,
                         llm_factory=lambda: llm)

    def test_mode_is_saved_and_passed_with_known_ids(self):
        runner = fake_collector_run(self.root, [dict(r) for r in ROWS[:1]])
        self.add_job("111")                              # a SEEK job whose JD we hold
        self.add_job("222", status="no_jd", jd=False)    # listed before, never read: read it again
        first = self.autopilot(runner)
        self.assertEqual(first.mode, "focused")
        self.assertEqual(first.set_mode("broad"), "broad")
        second = self.autopilot(runner)                  # a restart keeps the choice
        self.assertEqual(second.mode, "broad")
        with self.assertRaises(ValueError):
            second.set_mode("everything")
        report = second.tick(trigger="test", sources=("seek",))
        self.assertEqual(report["mode"], "broad")
        options = runner.options[-1]
        self.assertEqual(options[options.index("--mode") + 1], "broad")
        self.assertEqual(options[options.index("--worktypes") + 1], "242,244")
        self.assertEqual(options[options.index("--delay") + 1], "8.0")
        self.assertIn("Management", options[options.index("--exclude-subclass") + 1])
        self.assertEqual(runner.calls[-1], (self.settings.seek_broad_max_pages, self.settings.seek_broad_max_details))
        known = Path(options[options.index("--known-ids") + 1]).read_text(encoding="utf-8").split()
        self.assertEqual(known, ["111"])
        focused = second.tick(trigger="test", sources=("seek",), mode="focused")   # one round in the other mode
        self.assertEqual(focused["mode"], "focused")
        self.assertEqual(runner.calls[-1], (self.settings.seek_max_pages, self.settings.seek_max_details))
        self.assertEqual(second.mode, "broad")
        status = second.status()
        self.assertEqual((status["mode"], status["mode_limits"]["broad"]["max_details"]), ("broad", self.settings.seek_broad_max_details))

    def test_a_rate_limited_round_says_the_next_one_continues(self):
        def runner(collector_dir, max_pages, max_details, options=()):
            return 2, "", ""
        fake_collector_run(self.root, [dict(r) for r in ROWS[:1]])
        report_file = next((self.root / "collector" / "seek_output" / "runs").iterdir()) / "run_report.json"
        report_file.write_text(json.dumps({"rate_limited": True, "errors": ["Search page 5: HTTP 429"]}), encoding="utf-8")
        report = self.autopilot(runner).tick(trigger="test", sources=("seek",))
        self.assertTrue(report["sources"]["seek"]["rate_limited"])
        self.assertTrue(any("没有完整 JD 的结果不入账" in note for note in report["notes"]))
        self.assertEqual(report["errors"], [])

    def test_rescreen_annotates_decided_jobs_and_requeues_failed_ones(self):
        jd = {"Finance Partner": "Finance Business Partner\n\nPartner with the CFO on budgets.\n",
              "Junior Dev": "Junior Developer\n\nWe are hiring a junior developer. Python.\n",
              "Flaky Support": "Service Desk Analyst\n\nFirst-level support for staff.\n",
              "Skipped By Hand": "Junior Tester\n\nTest our apps.\n"}

        def fit(text):
            if "CFO" in text:
                return {**FIT_PLAIN, "role_family": "non_technical"}
            if "Service Desk" in text:
                return {**FIT_PLAIN, "role_family": "it_support"}
            return dict(FIT_PLAIN)
        llm = FakeLLM(lambda text: {"verdict": "unspecified", "quote": "", "reason": "x"}, fit=fit)
        autopilot = Autopilot(self.settings, self.sessions, self.worker, launch=lambda path: None,
                              llm_factory=lambda: llm)
        ids = {}
        for title, status in (("Finance Partner", "done"), ("Junior Dev", "done"), ("Flaky Support", "failed"),
                              ("Skipped By Hand", "screened_out")):
            ids[title] = self.add_job(title, status=status)
            (self.root / "jobs" / f"{title}.txt").write_text(jd[title], encoding="utf-8")
        with self.sessions() as session:
            session.get(Job, ids["Skipped By Hand"]).note = "手动跳过"
            session.commit()
        ids["no jd"] = self.add_job("no jd", status="done", jd=False)
        self.stub_generation()
        report = autopilot.rescreen(requeue_failed=True)
        decisions = {r["title"]: r["decision"] for r in report["rescreened"]}
        self.assertEqual(decisions, {"Finance Partner": "skip", "Junior Dev": "run", "Flaky Support": "run"})
        self.assertEqual(report["no_jd"], 1)
        self.assertEqual([r["title"] for r in report["queued"]], ["Flaky Support"])
        self.assertEqual(self.status_of(ids["Finance Partner"]), "done")        # annotated, not moved
        self.assertEqual(self.status_of(ids["Skipped By Hand"]), "screened_out")
        with self.sessions() as session:
            latest = session.scalars(select(Screening).where(Screening.job_id == ids["Finance Partner"])).all()[-1]
            self.assertEqual((latest.decision, latest.rule_result["fit"]["skips"]), ("skip", ["岗位类型：非技术岗位"]))
        self.worker.start()
        self.assertTrue(until(lambda: self.ran == [("Flaky Support", 1)]))
        self.assertTrue(until(lambda: self.status_of(ids["Flaky Support"]) == "done"))

    def test_full_time_only_when_contracts_are_not_accepted(self):
        settings = self.settings.model_copy(update={"accept_contract": False})
        options = seek.collector_options(settings, "focused")
        self.assertEqual(options[options.index("--worktypes") + 1], "242")
        self.assertNotIn("--known-ids", options)


if __name__ == "__main__":
    unittest.main()


class ScreeningRecoveryTests(WorkerCase):
    def autopilot_for_recovery(self):
        def fit(text):
            return {**FIT_PLAIN, "role_family": "non_technical" if "CFO" in text else "software_dev"}
        llm = FakeLLM(lambda text: {"verdict": "unspecified", "quote": "", "reason": "No restrictions"}, fit=fit)
        return Autopilot(self.settings, self.sessions, self.worker, llm_factory=lambda: llm)

    def test_repair_targets_latest_failures_and_refreshes_ready_decisions(self):
        bad = {"decision": "run_flagged", "flags": ["匹配度预筛失败：HTTP 400"], "role_family": "", "level": "unclear"}
        junior = self.add_job("Junior Dev", status="ready", fit=bad)
        finance = self.add_job("Finance Partner", status="ready", fit=bad)
        done = self.add_job("Completed Finance", status="done", fit=bad)
        healthy = self.add_job("Healthy", status="ready", fit={"decision": "run", "flags": ["普通需核对"], "role_family": "it_support"})
        skipped = self.add_job("Skipped", status="screened_out", fit=bad)
        for title in ("Finance Partner", "Completed Finance"):
            (self.root / "jobs" / (title + ".txt")).write_text("Partner with the CFO on budgets.", encoding="utf-8")
        with self.sessions() as session:
            session.get(Job, skipped).note = "手动跳过"
            session.commit()
        report = self.autopilot_for_recovery().rescreen(only_failed=True)
        self.assertEqual({r["job_id"] for r in report["rescreened"]}, {junior, finance, done})
        self.assertEqual(self.status_of(junior), "ready")
        self.assertEqual(self.status_of(finance), "screened_out")
        self.assertEqual(self.status_of(done), "done")
        self.assertEqual(self.status_of(healthy), "ready")
        self.assertEqual(report["queued"], [])
        self.assertEqual(report["errors"], [])
        with self.sessions() as session:
            j = session.get(Job, junior)
            latest = max(j.screenings, key=lambda r: (r.created_at, r.id))
            self.assertEqual(latest.rule_result["work_rights"]["processing_status"], "ok")
            self.assertEqual(latest.rule_result["fit"]["processing_status"], "ok")
        again = self.autopilot_for_recovery().rescreen(only_failed=True)
        self.assertEqual(again["rescreened"], [])

    def test_rights_errors_are_persisted_and_not_confused_with_ambiguous_requirements(self):
        from local_llm import LLMError
        from backend.app.services.autopilot import screening_failed
        class Failing:
            def generate_json(self, *args, **kwargs):
                raise LLMError("HTTP 400", error_type="http_error")
        job_id = self.add_job("Legacy Error", status="ready", fit={"flags": ["匹配度预筛失败：HTTP 400"]})
        report = Autopilot(self.settings, self.sessions, self.worker, llm_factory=Failing).rescreen(only_failed=True)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(self.status_of(job_id), "ready")
        with self.sessions() as session:
            latest = max(session.get(Job, job_id).screenings, key=lambda r: (r.created_at, r.id))
            self.assertTrue(screening_failed(latest))
            self.assertEqual(latest.rule_result["work_rights"]["processing_status"], "error")
        ambiguous = Screening(rule_result={"work_rights": {"processing_status": "ok", "reason": "Requirement ambiguous"}, "fit": {"decision": "run", "flags": ["普通提示"]}}, model_verdict="unclear")
        self.assertFalse(screening_failed(ambiguous))
