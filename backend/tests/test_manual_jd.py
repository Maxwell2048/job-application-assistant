"""Details before search pages, and JDs pasted by hand: the ledger's title-only
jobs go to the collector first, a pasted JD is recorded and screened like a
collected one, and a pasted new JD can be screened instead of generated. No
network, no subprocesses."""
import sys
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.app.db.models import Job  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.app.services.autopilot import Autopilot  # noqa: E402
from backend.tests.test_api import FakeRunner  # noqa: E402
from backend.tests.test_autopilot import ROWS, FakeLLM, fake_collector_run  # noqa: E402
from backend.tests.test_worker import WorkerCase, until  # noqa: E402

JD = ("About the role\nWe are hiring a junior developer to build Python services in Perth.\n"
      "You will work with a friendly team and learn from senior engineers.\n")


def rights(text):
    return {"verdict": "unspecified", "quote": "", "reason": "没提到"}


class PendingIdsTests(WorkerCase):
    def test_title_only_jobs_go_first_junior_titles_before_others(self):
        runner = fake_collector_run(self.root, [dict(ROWS[0])])
        older = self.add_job("Systems Engineer", status="no_jd", jd=False)
        junior = self.add_job("Junior Analyst", status="no_jd", jd=False)
        skipped = self.add_job("Graduate Tester", status="screened_out", jd=False)   # skipped by hand
        self.add_job("Has A JD", status="done")
        with self.sessions() as session:
            ids = {j.id: j.external_id for j in session.query(Job).all()}
        autopilot = Autopilot(self.settings, self.sessions, self.worker, launch=lambda path: None, seek_runner=runner,
                              llm_factory=lambda: FakeLLM(rights))
        autopilot.tick(trigger="test", sources=("seek",), generate=False)
        options = runner.options[-1]
        pending = Path(options[options.index("--pending-ids") + 1]).read_text(encoding="utf-8").splitlines()
        self.assertEqual(pending, [ids[older], ids[junior]])  # explicit IT title first; junior alone is not IT evidence
        self.assertNotIn(ids[skipped], pending)


class ManualJdTests(WorkerCase):
    def client(self):
        app = create_app(self.settings, runner=FakeRunner(self.root / "outputs"),
                         autopilot_kwargs={"launch": lambda path: None, "llm_factory": lambda: FakeLLM(rights)})
        app.state.worker.pipeline.model_online = lambda: (True, "fake")
        app.state.autopilot.set_auto_generate(False)     # screened jobs wait in "ready"
        self.app = app
        return TestClient(app)

    def wait_for_round(self):
        time.sleep(0.05)
        self.assertTrue(until(lambda: not self.app.state.autopilot.running))

    def test_pasting_the_jd_of_a_title_only_job_screens_it(self):
        job_id = self.add_job("Junior Developer", status="no_jd", jd=False)
        with self.sessions() as session:
            job = session.get(Job, job_id)
            job.company, job.url = "Acme", "https://au.seek.com/job/555"
            session.commit()
        with self.client() as client:
            self.assertEqual(client.post(f"/api/jobs/{job_id}/jd", json={"jd_text": "too short"}).status_code, 400)
            answer = client.post(f"/api/jobs/{job_id}/jd", json={"jd_text": JD})
            self.assertEqual(answer.status_code, 202, answer.text)
            self.assertEqual(answer.json()["screening"], "started")
            self.wait_for_round()
            self.assertTrue(until(lambda: self.status_of(job_id) == "ready"))
            detail = client.get(f"/api/jobs/{job_id}").json()
            self.assertIn("Company: Acme", detail["jd_text"])                # the listing facts travel with it
            self.assertIn("junior developer to build Python", detail["jd_text"])
            self.assertIn("JD 为手动粘贴", detail["note"])
            self.assertEqual(detail["screening"]["decision"], "run")
            self.assertEqual(client.post(f"/api/jobs/{job_id}/jd", json={"jd_text": JD}).status_code, 409)
            self.assertEqual(client.post("/api/jobs/999/jd", json={"jd_text": JD}).status_code, 404)

    def test_a_pasted_new_jd_can_be_screened_instead_of_generated(self):
        with self.client() as client:
            answer = client.post("/api/jobs", json={"jd_text": JD, "jd_name": "Acme", "screen_first": True})
            self.assertEqual(answer.status_code, 202, answer.text)
            job_id = answer.json()["job_id"]
            self.wait_for_round()
            self.assertTrue(until(lambda: self.status_of(job_id) == "ready"))
            self.assertEqual(client.get("/api/tasks").json(), [])            # nothing was generated
            again = client.post("/api/jobs", json={"jd_text": JD, "screen_first": True})
            self.assertEqual(again.status_code, 409)                         # the same text is the same job


if __name__ == "__main__":
    unittest.main()
