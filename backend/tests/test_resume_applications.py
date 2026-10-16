"""Version-specific application state and legacy runs; no model calls."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.tests.test_api import ApiTestCase, FakeRunner
from backend.app.db.models import ResumeApplication, Job, RagRun


class ResumeApplicationTests(ApiTestCase):
    def make_run(self, run_id, resume=True):
        run = self.outputs / run_id
        run.mkdir(parents=True, exist_ok=True)
        (run / "run_meta.json").write_text(json.dumps({"run_id": run_id, "run_status": "complete",
            "input": {"jd_path": "jobs/example.txt"}}), encoding="utf-8")
        (run / "report.md").write_text("# Report", encoding="utf-8")
        if resume:
            (run / "resume_tailored.md").write_text("# Alex Sample", encoding="utf-8")
        return run

    def test_old_runs_default_unsubmitted_and_filter_before_limit(self):
        client = self.start(FakeRunner(self.outputs))
        self.make_run("20261013-120000-aaaaaa", resume=False)
        self.make_run("20261012-230000-bbbbbb")
        rows = client.get("/api/runs?resumes_only=true&limit=1").json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["generation_date"], "2026-10-12")
        self.assertEqual(rows[0]["application_status"], "unsubmitted")
        self.assertIsNone(rows[0]["submitted_at"])
        self.assertEqual(client.get("/api/runs?limit=-1").status_code, 422)

    def test_submission_is_idempotent_reversible_and_persistent(self):
        client = self.start(FakeRunner(self.outputs))
        run_id = "20261013-120000-aaaaaa"
        self.make_run(run_id)
        url = f"/api/runs/{run_id}/application"
        first = client.put(url, json={"submitted": True}).json()
        again = client.put(url, json={"submitted": True}).json()
        self.assertEqual(first["application_status"], "submitted")
        self.assertEqual(first["submitted_at"], again["submitted_at"])
        self.assertTrue(first["submitted_at"].endswith("+00:00"))
        self.assertEqual(client.get(f"/api/runs/{run_id}").json()["application_status"], "submitted")
        with client.app.state.session_factory() as session:
            self.assertIsNotNone(session.get(ResumeApplication, run_id).submitted_at)
        self.assertEqual(client.get("/api/runs?resumes_only=true").json()[0]["application_status"], "submitted")
        cleared = client.put(url, json={"submitted": False}).json()
        self.assertEqual(cleared["application_status"], "unsubmitted")
        self.assertIsNone(cleared["submitted_at"])

    def test_other_version_and_job_generation_state_are_independent(self):
        client = self.start(FakeRunner(self.outputs))
        old, new = "20261012-120000-aaaaaa", "20261013-120000-bbbbbb"
        self.make_run(old); self.make_run(new)
        with client.app.state.session_factory() as session:
            job = Job(source="manual", external_id="test", title="Developer", company="Example", status="done")
            session.add(job); session.flush()
            job_id = job.id
            session.add_all([RagRun(job_id=job.id, run_id=old), RagRun(job_id=job.id, run_id=new)])
            session.commit()
        client.put(f"/api/runs/{old}/application", json={"submitted": True})
        self.assertEqual(client.get(f"/api/runs/{new}").json()["application_status"], "unsubmitted")
        job = client.get(f"/api/jobs/{job_id}").json()
        self.assertEqual(job["status"], "done")
        statuses = {r["run_id"]: r["application_status"] for r in job["runs"]}
        self.assertEqual(statuses, {old: "submitted", new: "unsubmitted"})
        rows = client.get("/api/runs?resumes_only=true").json()
        self.assertTrue(all(r["job_id"] == job_id and r["company"] == "Example" for r in rows))

    def test_archive_state_independent_of_job_page(self):
        client = self.start(FakeRunner(self.outputs))
        run_id = "20261013-120000-aaaaaa"
        self.make_run(run_id)
        with client.app.state.session_factory() as session:
            job = Job(source="seek", external_id="archived", title="Old skipped job", status="screened_out")
            session.add(job); session.flush()
            session.add(RagRun(job_id=job.id, run_id=run_id, run_status="complete"))
            for i in range(205):
                session.add(Job(source="seek", external_id=str(i), title="New job", status="no_jd"))
            session.commit()
            archived_id = job.id
        first = client.get("/api/jobs?limit=200").json()
        self.assertNotIn(archived_id, [j["id"] for j in first])
        self.assertEqual(len(client.get("/api/jobs?limit=200&offset=200").json()), 6)
        row = client.get("/api/runs?resumes_only=true").json()[0]
        self.assertTrue(row["archived"])
        self.assertEqual(row["job_status"], "screened_out")
        self.assertEqual(row["application_status"], "unsubmitted")

    def test_missing_resume_and_invalid_inputs_cannot_be_marked(self):
        client = self.start(FakeRunner(self.outputs))
        run_id = "20261013-120000-aaaaaa"
        self.make_run(run_id, resume=False)
        self.assertEqual(client.put(f"/api/runs/{run_id}/application", json={"submitted": True}).status_code, 409)
        self.assertEqual(client.put("/api/runs/20261013-120000-bbbbbb/application", json={"submitted": True}).status_code, 404)
        self.assertEqual(client.put(f"/api/runs/{run_id}/application", json={"submitted": "true"}).status_code, 422)
        with client.app.state.session_factory() as session:
            self.assertEqual(session.query(ResumeApplication).count(), 0)

    def test_status_survives_app_recreation(self):
        from fastapi.testclient import TestClient
        from backend.app.main import create_app
        client = self.start(FakeRunner(self.outputs))
        run_id = "20261013-120000-aaaaaa"
        self.make_run(run_id)
        client.put(f"/api/runs/{run_id}/application", json={"submitted": True})
        app = create_app(self.settings, runner=FakeRunner(self.outputs))
        with TestClient(app) as restarted:
            self.assertEqual(restarted.get(f"/api/runs/{run_id}").json()["application_status"], "submitted")


class ResumeApplicationMigrationTests(unittest.TestCase):
    def test_upgrade_is_compatible_with_startup_created_tables(self):
        import importlib.util
        from unittest.mock import patch
        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        file = Path(__file__).resolve().parents[1] / "alembic/versions/a20261013_resume_applications.py"
        spec = importlib.util.spec_from_file_location("resume_migration", file)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite://")
        with engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            with patch.object(migration, "op", operations):
                migration.upgrade()
                self.assertTrue(sa.inspect(connection).has_table("resume_applications"))
                migration.upgrade()  # startup may create the table before Alembic catches up
                migration.downgrade()
                self.assertFalse(sa.inspect(connection).has_table("resume_applications"))
        engine.dispose()
