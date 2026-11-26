"""Run summary index and list-endpoint scaling: summaries are cached by a file
signature (memory and table), stay identical to run_summary(), and the list
endpoints issue the same number of queries however many jobs there are."""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.app.core.config import Settings  # noqa: E402
from backend.app.db.models import Job, RagRun, ResumeApplication, Screening  # noqa: E402
from backend.app.db.session import create_schema, make_engine, make_session_factory  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.app.services import run_index as run_index_module  # noqa: E402
from backend.app.services.run_index import RunIndex, scan_run_dir  # noqa: E402
from backend.app.services.runs import run_summary  # noqa: E402

DOCX = "Alex_Sample_Resume_Acme_Data-Engineer_0912-0000.docx"


def write_run(outputs, run_id, verdicts=("direct", "related"), resume=True):
    run = Path(outputs) / run_id
    run.mkdir(parents=True, exist_ok=True)
    (run / "run_meta.json").write_text(json.dumps({"run_id": run_id, "run_status": "complete",
                                                   "started_at": "2026-09-12T00:00:00",
                                                   "input": {"jd_path": "jobs/x.txt"}}), encoding="utf-8")
    requirements = [{"requirement_id": f"R{i:03d}", "category": "technical_skill", "importance": "required"}
                    for i, _ in enumerate(verdicts, 1)]
    matches = [{"requirement_id": f"R{i:03d}", "processing_status": "ok", "verdict": v}
               for i, v in enumerate(verdicts, 1)]
    (run / "requirements.json").write_text(json.dumps({"requirements": requirements}), encoding="utf-8")
    (run / "matches.json").write_text(json.dumps({"matches": matches}), encoding="utf-8")
    (run / "report.md").write_text("# report\n", encoding="utf-8")
    if resume:
        (run / "resume_tailored.md").write_text("# ALEX SAMPLE\n", encoding="utf-8")
        (run / DOCX).write_bytes(b"PK")
    return run


def touch_later(path, text):
    """Rewrite a file and move its mtime forward, so the change is visible even
    on file systems with coarse timestamps."""
    path.write_text(text, encoding="utf-8")
    later = time.time() + 5
    os.utime(path, (later, later))


class IndexTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.engine = make_engine(f"sqlite:///{(self.root / 'state.sqlite').as_posix()}")
        self.addCleanup(self.engine.dispose)
        create_schema(self.engine)
        self.sessions = make_session_factory(self.engine)
        self.outputs = self.root / "outputs"
        self.run = write_run(self.outputs, "20260912-000000-aaaaaa")

    def counting(self):
        calls = []

        def spy(run_dir):
            calls.append(Path(run_dir).name)
            return run_summary(run_dir)
        return calls, mock.patch.object(run_index_module, "run_summary", side_effect=spy)

    def test_summary_is_exactly_run_summary(self):
        index = RunIndex(self.sessions)
        self.assertEqual(index.summary(self.run), run_summary(self.run))

    def test_unchanged_runs_are_not_read_again(self):
        index = RunIndex(self.sessions)
        calls, patch = self.counting()
        with patch:
            index.summaries([self.run])
            index.summaries([self.run])
            index.summaries([self.run])
        self.assertEqual(calls, [self.run.name])

    def test_a_changed_file_rebuilds_the_summary(self):
        index = RunIndex(self.sessions)
        self.assertEqual(index.summary(self.run)["counts"]["related"], 1)
        touch_later(self.run / "matches.json", json.dumps({"matches": [
            {"requirement_id": "R001", "processing_status": "ok", "verdict": "direct"},
            {"requirement_id": "R002", "processing_status": "ok", "verdict": "direct"}]}))
        self.assertEqual(index.summary(self.run)["counts"], {"direct": 2, "related": 0, "insufficient": 0, "error": 0})

    def test_a_new_export_or_deleted_file_rebuilds_the_summary(self):
        index = RunIndex(self.sessions)
        self.assertNotIn("interview_prep", [d["stem"] for d in index.summary(self.run)["docs"]])
        (self.run / "interview_prep.md").write_text("# prep\n", encoding="utf-8")
        self.assertIn("interview_prep", [d["stem"] for d in index.summary(self.run)["docs"]])
        (self.run / "interview_prep.md").unlink()
        self.assertNotIn("interview_prep", [d["stem"] for d in index.summary(self.run)["docs"]])

    def test_a_restarted_backend_reuses_stored_summaries(self):
        RunIndex(self.sessions).summaries([self.run])
        calls, patch = self.counting()
        with patch:
            summary = RunIndex(self.sessions).summary(self.run)       # a new process: empty memory
        self.assertEqual(calls, [])
        self.assertEqual(summary, run_summary(self.run))

    def test_a_new_summary_version_rebuilds_stored_rows(self):
        RunIndex(self.sessions, version=1).summaries([self.run])
        calls, patch = self.counting()
        with patch:
            RunIndex(self.sessions, version=2).summaries([self.run])
        self.assertEqual(calls, [self.run.name])

    def test_callers_get_copies(self):
        index = RunIndex(self.sessions)
        index.summary(self.run)["title"] = "changed by a caller"
        self.assertNotIn("title", index.summary(self.run))

    def test_missing_directories_are_left_out(self):
        index = RunIndex(self.sessions)
        self.assertEqual(index.summaries([self.outputs / "20260912-000000-ffffff"]), {})
        self.assertIsNone(scan_run_dir(self.outputs / "20260912-000000-ffffff"))

    def test_an_unwritable_cache_still_returns_correct_summaries(self):
        def broken():
            raise OperationalError("SELECT", {}, Exception("database is locked"))
        with self.assertLogs("backend.app.services.run_index", level="WARNING"):
            summary = RunIndex(broken).summary(self.run)
        self.assertEqual(summary, run_summary(self.run))

    def test_many_runs_are_stored_and_read_in_chunks(self):
        runs = [write_run(self.outputs, f"20260913-{i:06d}-bbbbbb", resume=False) for i in range(620)]
        RunIndex(self.sessions).summaries(runs)
        calls, patch = self.counting()
        with patch:
            found = RunIndex(self.sessions).summaries(runs)
        self.assertEqual((len(found), calls), (620, []))


class SchemaTests(unittest.TestCase):
    def test_start_up_adds_indexes_missing_from_an_older_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(f"sqlite:///{(Path(tmp) / 'old.sqlite').as_posix()}")
            try:
                create_schema(engine)
                with engine.begin() as connection:
                    connection.exec_driver_sql("DROP INDEX ix_jobs_last_seen")
                    connection.exec_driver_sql("DROP TABLE run_summary_cache")
                create_schema(engine)
                create_schema(engine)                      # idempotent
                with engine.connect() as connection:
                    names = {row[0] for row in connection.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE name IN ('ix_jobs_last_seen', 'run_summary_cache')")}
            finally:
                engine.dispose()
        self.assertEqual(names, {"ix_jobs_last_seen", "run_summary_cache"})


class ListScalingTests(unittest.TestCase):
    """The board and the resume list must not cost a query (or a directory read) per job."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.outputs = root / "outputs"
        self.jobs_dir = root / "jobs"
        self.settings = Settings(_env_file=None, project_root=root, jobs_dir=self.jobs_dir, outputs_dir=self.outputs,
                                 resume_path=root / "r.docx", config_path=root / "c.json", frontend_dist=root / "nodist",
                                 database_url=f"sqlite:///{(root / 'state.sqlite').as_posix()}")
        self.app = create_app(self.settings)       # no lifespan: worker and autopilot are not started
        self.addCleanup(self.app.state.engine.dispose)
        self.client = TestClient(self.app)
        self.statements = []
        event.listen(self.app.state.engine, "before_cursor_execute",
                     lambda *args: self.statements.append(args[2]))
        self.next_id = 0

    def add_jobs(self, count):
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        with self.app.state.session_factory() as session:
            for _ in range(count):
                self.next_id += 1
                n = self.next_id
                job = Job(source="seek", external_id=str(n), title=f"Role {n}", company="Example",
                          status="done", last_seen=start + timedelta(minutes=n))
                session.add(job)
                session.flush()
                session.add(Screening(job_id=job.id, decision="run", rule_result={}))
                run_id = f"20260912-{n:06d}-abcdef"
                write_run(self.outputs, run_id)
                session.add(RagRun(job_id=job.id, run_id=run_id, run_status="complete"))
                if n % 2:
                    session.add(ResumeApplication(run_id=run_id, submitted_at=start))
            session.commit()

    def queries_for(self, path):
        self.client.get(path)                       # warm the run index, as after the first page load
        self.statements.clear()
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return len(self.statements), response.json()

    def test_job_list_queries_do_not_grow_with_jobs(self):
        self.add_jobs(3)
        few, body = self.queries_for("/api/jobs?limit=1000")
        self.assertEqual(len(body), 3)
        self.add_jobs(40)
        many, body = self.queries_for("/api/jobs?limit=1000")
        self.assertEqual(len(body), 43)
        self.assertEqual(few, many)

    def test_job_list_content_matches_per_run_summaries(self):
        self.add_jobs(4)
        body = self.client.get("/api/jobs?limit=1000").json()
        for job in body:
            run = job["runs"][0]
            expected = run_summary(self.outputs / run["run_id"])
            for key in ("counts", "coverage", "docs", "downloads", "has_resume", "can_interview"):
                self.assertEqual(run[key], expected[key], key)
            self.assertEqual(run["application_status"], "submitted" if int(job["external_id"]) % 2 else "unsubmitted")

    def test_run_list_queries_do_not_grow_with_runs(self):
        self.add_jobs(3)
        few, body = self.queries_for("/api/runs?resumes_only=true&limit=5000")
        self.assertEqual(len(body), 3)
        self.add_jobs(40)
        many, body = self.queries_for("/api/runs?resumes_only=true&limit=5000")
        self.assertEqual(len(body), 43)
        self.assertEqual(few, many)

    def test_a_run_finished_after_the_first_load_shows_its_new_files(self):
        self.add_jobs(1)
        run_id = self.client.get("/api/runs").json()[0]["run_id"]
        self.assertFalse(any(d["stem"] == "interview_prep" for d in self.client.get("/api/runs").json()[0]["docs"]))
        (self.outputs / run_id / "interview_prep.md").write_text("# prep\n", encoding="utf-8")
        docs = self.client.get("/api/runs").json()[0]["docs"]
        self.assertTrue(any(d["stem"] == "interview_prep" for d in docs))


if __name__ == "__main__":
    unittest.main()
