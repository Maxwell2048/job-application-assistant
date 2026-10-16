"""Search-only rounds and the "ready" column: collect and screen without
generating, queue the ready jobs later, keep the choice across restarts,
re-link JD files after the project folder moved, and tell open boards when a
job is skipped. No network, no subprocesses."""
import json
import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.app.db.models import Job  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.app.services.autopilot import Autopilot  # noqa: E402
from backend.app.services.ledger import relink_jd_files  # noqa: E402
from backend.tests.test_api import FakeRunner, wait_for  # noqa: E402
from backend.tests.test_autopilot import FIT_PLAIN, ROWS, FakeLLM, fake_collector_run  # noqa: E402
from backend.tests.test_worker import WorkerCase, until  # noqa: E402

UNSPECIFIED = {"verdict": "unspecified", "quote": "", "reason": "没提到"}


class SearchOnlyTests(WorkerCase):
    def autopilot(self, runner=None, fit=None):
        def rights(text):   # the work-rights answer a careful model gives
            if "New Zealand Citizen" in text:
                return {"verdict": "citizen_or_pr", "quote": "are either Australian or New Zealand Citizen",
                        "reason": "公民"}
            return dict(UNSPECIFIED)
        llm = FakeLLM(rights, fit=fit)
        return Autopilot(self.settings, self.sessions, self.worker, launch=lambda path: None, seek_runner=runner,
                         llm_factory=lambda: llm)

    def test_a_search_only_round_leaves_eligible_jobs_ready(self):
        runner = fake_collector_run(self.root, [dict(r) for r in ROWS])
        self.stub_generation()
        report = self.autopilot(runner).tick(trigger="test", sources=("seek",), generate=False)
        self.assertFalse(report["generate"])
        self.assertEqual(report["queued"], [])
        self.assertEqual([b["title"] for b in report["ready"]], ["Junior Software Developer"])
        self.assertEqual(self.worker.queue_status()["waiting"], [])
        with self.sessions() as session:
            statuses = {j.external_id: j.status for j in session.query(Job).all()}
        self.assertEqual(statuses, {"101": "ready", "102": "screened_out", "103": "screened_out"})
        # later: generate every ready job at once
        self.assertEqual(self.worker.generate_ready(), 1)
        self.worker.start()
        self.assertTrue(until(lambda: self.ran == [("Acme", 1)]))
        self.assertTrue(until(lambda: self.status_of(1) == "done"))
        self.assertEqual(self.worker.generate_ready(), 0)

    def test_the_choice_is_saved_and_old_preference_files_still_load(self):
        first = self.autopilot()
        self.assertTrue(first.auto_generate)                         # the setting's default
        self.assertFalse(first.set_auto_generate(False))
        self.assertEqual(first.set_mode("broad"), "broad")
        again = self.autopilot()
        self.assertEqual((again.mode, again.auto_generate), ("broad", False))
        self.assertEqual(again.status()["auto_generate"], False)
        # a file written before this choice existed holds only the mode
        (self.root / "state" / "autopilot_mode.json").write_text(json.dumps({"mode": "broad"}), encoding="utf-8")
        old = self.autopilot()
        self.assertEqual((old.mode, old.auto_generate), ("broad", True))
        # an unset round follows the saved choice
        runner = fake_collector_run(self.root, [dict(ROWS[0])])
        again.seek_runner = runner
        self.assertEqual(len(again.tick(trigger="test", sources=("seek",))["ready"]), 1)

    def test_jd_files_are_relinked_after_the_project_moved(self):
        moved = self.add_job("moved")
        with self.sessions() as session:
            session.get(Job, moved).jd_path = r"Z:\example\old-project\jobs\moved.txt"   # the old drive
            session.commit()
        lost = self.add_job("lost", jd=False)
        with self.sessions() as session:
            session.get(Job, lost).jd_path = r"Z:\example\old-project\jobs\gone.txt"
            session.commit()
        self.assertEqual(relink_jd_files(self.sessions, self.root / "jobs"), 1)
        with self.sessions() as session:
            self.assertEqual(Path(session.get(Job, moved).jd_path), self.root / "jobs" / "moved.txt")
            self.assertTrue(session.get(Job, lost).jd_path.endswith("gone.txt"))   # nothing to point at: unchanged
        self.assertEqual(relink_jd_files(self.sessions, self.root / "jobs"), 0)


class BoardApiTests(WorkerCase):
    def app(self):
        app = create_app(self.settings, runner=FakeRunner(self.root / "outputs"),
                         autopilot_kwargs={"launch": lambda path: None})
        app.state.worker.pipeline.model_online = lambda: (True, "fake")
        return app

    def test_skip_and_generate_ready_tell_open_boards(self):
        skipped = self.add_job("only-a-title", status="no_jd", jd=False)
        ready = self.add_job("ready-one", status="ready")
        app = self.app()
        events = app.state.worker.events.subscribe()
        with TestClient(app) as client:
            answer = client.post(f"/api/jobs/{skipped}/decision", json={"action": "skip"})
            self.assertEqual(answer.json()["status"], "screened_out")
            self.assertEqual(events.get(timeout=2), {"type": "jobs", "data": {"id": skipped, "status": "screened_out"}})
            self.assertEqual(client.post("/api/jobs/generate-ready").json(), {"queued": 1})
            task = next(t for t in client.get("/api/tasks").json() if t["job_id"] == ready)
            self.assertEqual(wait_for(client, task["id"])["status"], "done")
            self.assertEqual(client.post("/api/autopilot/generate", json={"enabled": False}).json(),
                             {"auto_generate": False})
            self.assertFalse(client.get("/api/autopilot/status").json()["auto_generate"])


if __name__ == "__main__":
    unittest.main()
