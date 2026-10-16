"""Autopilot tests: screening rules and model gate, ledger dedupe, the tick
against a fake collector run and a fake model. No network, no subprocesses."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.app.core.config import Settings  # noqa: E402
from backend.app.db.session import create_schema, make_engine, make_session_factory  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.app.services import screen  # noqa: E402
from backend.app.services.ledger import ingest  # noqa: E402
from backend.app.services.sources import inbox, seek  # noqa: E402
from backend.tests.test_api import FakeRunner, wait_for  # noqa: E402
from local_llm import LLMError  # noqa: E402

JD_OK = ("Junior Software Developer\n\nAbout the role\nYou will build Python services in Perth.\n"
         "You must have full working rights in Australia; no visa sponsorship is available.\n") * 2
JD_CITIZEN = ("Graduate Program 2027\n\nIf you have a university degree and are either Australian or "
              "New Zealand Citizen, our Graduate Program could be a great fit for you.\nBased in Perth.\n") * 2
JD_CLEARANCE = ("Junior Analyst\n\nDue to the nature of the work you must be able to obtain and maintain an "
                "Australian Government Baseline security clearance.\nPerth office.\n") * 2


FIT_PLAIN = {"level": "unclear", "level_quote": "", "min_years": 0, "years_mandatory": False, "years_quote": "",
             "role_family": "software_dev", "required_stack": [], "degree": "unspecified", "degree_quote": "",
             "constraints": [], "reason": "普通开发岗位"}


class FakeLLM:
    """Returns canned answers (a list in order, or a function of the JD text);
    runs the program-side validation like the real adapter.

    The work-rights screen reads `answers` and is what `calls` counts; the fit
    screen reads `fit` (same forms), by default a plain software role."""

    def __init__(self, answers, fit=None):
        self.answers = answers if callable(answers) else list(answers)
        self.fit = fit if fit is None or callable(fit) else list(fit)
        self.calls = 0
        self.fit_calls = 0

    def generate_json(self, task, system_prompt, payload, schema=None, timeout_seconds=None, validate=None):
        if task == "fit_screen":
            self.fit_calls += 1
            source = self.fit if self.fit is not None else (lambda jd: dict(FIT_PLAIN))
        else:
            self.calls += 1
            source = self.answers
        last = None
        for _attempt in range(3):  # like the real adapter: three attempts, validation errors fed back
            if callable(source):
                answer = source(payload.get("jd_text", ""))
            elif not source:
                raise LLMError(last or "no answer", error_type="structure" if last else "service_unavailable")
            else:
                answer = source.pop(0)
            if isinstance(answer, Exception):
                raise answer
            errors = validate(answer) if validate else []
            if not errors:
                return answer
            last = "；".join(errors)
        raise LLMError(last, error_type="structure")


def settings_for(root):
    return Settings(_env_file=None, project_root=root, jobs_dir=root / "jobs", inbox_dir=root / "jobs" / "inbox",
                    outputs_dir=root / "outputs", resume_path=root / "r.docx", config_path=root / "c.json",
                    frontend_dist=root / "nodist", database_url=f"sqlite:///{(root / 'state.sqlite').as_posix()}",
                    collector_dir=root / "collector", profile_path=root / "profile.md")


class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(_env_file=None, config_path=Path("c.json"))

    def test_rules(self):
        base = {"title": "Junior Developer", "location": "Perth WA", "work_type": "Full time", "jd_text": JD_OK}
        self.assertFalse(screen.rule_screen(base, self.settings)["exclude"])
        self.assertIn("资深", screen.rule_screen({**base, "title": "Senior Developer"}, self.settings)["reasons"][0])
        self.assertIn("FIFO", screen.rule_screen({**base, "jd_text": "FIFO roster 8/6."}, self.settings)["reasons"][0])
        self.assertIn("不在 Perth", screen.rule_screen({**base, "location": "Sydney NSW"}, self.settings)["reasons"][0])
        remote = screen.rule_screen({**base, "location": "Sydney NSW", "jd_text": "Fully remote role, work from anywhere in Australia."}, self.settings)
        self.assertFalse(remote["exclude"])
        self.assertTrue(remote["flags"])
        contract = screen.rule_screen({**base, "work_type": "Contract/Temp"}, self.settings)
        self.assertEqual((contract["exclude"], contract["flags"]), (False, ["合同或临时岗位（Contract/Temp）"]))
        self.assertIn("非全职", screen.rule_screen({**base, "work_type": "Part time"}, self.settings)["reasons"][0])
        no_contract = self.settings.model_copy(update={"accept_contract": False})
        self.assertIn("非全职", screen.rule_screen({**base, "work_type": "Contract/Temp"}, no_contract)["reasons"][0])
        self.assertTrue(screen.rule_screen({**base, "expired": True}, self.settings)["exclude"])
        self.assertIn("3 年以上", screen.rule_screen({**base, "jd_text": "5+ years of experience required."},
                                                   self.settings)["flags"][0])

    def test_rule_second_opinion(self):
        self.assertTrue(screen.restricted_by_rule("Applicants must be Australian citizens or permanent residents."))
        self.assertTrue(screen.restricted_by_rule("You must be either Australian or New Zealand Citizen to apply."))
        self.assertEqual(screen.restricted_by_rule("Australian citizenship is not required."), "")
        self.assertEqual(screen.restricted_by_rule("Must be an Australian citizen or hold a valid visa."), "")
        # "permanent" work rights exclude temporary visa holders; plain full work rights do not
        self.assertTrue(screen.restricted_by_rule("You must have permanent rights to work in Australia."))
        self.assertTrue(screen.restricted_by_rule("Applicants need permanent full working rights."))
        self.assertEqual(screen.restricted_by_rule("You must have full working rights in Australia."), "")
        self.assertEqual(screen.restricted_by_rule("This is a permanent full-time position in Perth."), "")

    def test_permanent_work_rights_are_never_waved_through(self):
        jd = "Full Stack Developer\n\nYou must have permanent rights to work in Australia.\nBased in Perth.\n" * 2
        # the model under-reads it: the rule's second opinion turns that into a flagged run
        lenient = FakeLLM([{"verdict": "full_rights_ok", "quote": "permanent rights to work in Australia",
                            "reason": "有工作权利即可"}])
        gate = screen.work_rights_gate(jd, lenient)
        self.assertEqual((gate["verdict"], gate["decision"]), ("unclear", "run_flagged"))
        # the model reads it correctly: skipped, and the quote passes the keyword check
        strict = FakeLLM([{"verdict": "citizen_or_pr", "quote": "You must have permanent rights to work in Australia.",
                           "reason": "要求永久工作权利"}])
        gate = screen.work_rights_gate(jd, strict)
        self.assertEqual((gate["verdict"], gate["decision"]), ("citizen_or_pr", "skip"))

    def test_gate_explicit_restriction_is_skipped_with_verified_quote(self):
        llm = FakeLLM([{"verdict": "citizen_or_pr", "quote": "are either Australian or New Zealand Citizen",
                        "reason": "明确要求公民"}])
        gate = screen.work_rights_gate(JD_CITIZEN, llm)
        self.assertEqual((gate["verdict"], gate["decision"]), ("citizen_or_pr", "skip"))
        self.assertIn(gate["quote"], JD_CITIZEN)

    def test_gate_rejects_invented_or_unrelated_quotes(self):
        # 1st answer: quote not in the JD; 2nd: restrictive verdict with a quote that has no identity words;
        # 3rd: valid.
        llm = FakeLLM([{"verdict": "citizen_or_pr", "quote": "Australian citizens only", "reason": "x"},
                       {"verdict": "clearance", "quote": "Perth office.", "reason": "x"},
                       {"verdict": "clearance", "quote": "Baseline security clearance", "reason": "x"}])
        gate = screen.work_rights_gate(JD_CLEARANCE, llm)
        self.assertEqual(gate["decision"], "skip")
        self.assertEqual(llm.answers, [])  # all three attempts were needed

    def test_gate_disagreement_runs_flagged(self):
        # The model says fine, the rule finds an explicit clause: run, but flagged.
        llm = FakeLLM([{"verdict": "unspecified", "quote": "", "reason": "没提到"}])
        gate = screen.work_rights_gate(JD_CITIZEN.replace("are either Australian or New Zealand Citizen",
                                                          "must be Australian citizens"), llm)
        self.assertEqual((gate["verdict"], gate["decision"]), ("unclear", "run_flagged"))
        self.assertIn("must be Australian citizens", gate["quote"])

    def test_gate_ok_and_model_failure(self):
        llm = FakeLLM([{"verdict": "no_sponsorship_ok", "quote": "no visa sponsorship is available", "reason": "ok"}])
        self.assertEqual(screen.work_rights_gate(JD_OK, llm)["decision"], "run")
        failing = FakeLLM([])
        gate = screen.work_rights_gate(JD_OK, failing)
        self.assertEqual(gate["decision"], "run_flagged")
        self.assertIn("模型判断失败", gate["reason"])


class LedgerTests(unittest.TestCase):
    def test_dedupe_update_and_repost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = make_engine(f"sqlite:///{(root / 's.sqlite').as_posix()}")
            create_schema(engine)
            factory = make_session_factory(engine)
            item = {"source": "seek", "external_id": "1", "title": "Junior Dev", "company": "Acme", "url": "u",
                    "location": "Perth WA", "work_type": "Full time", "listed_at": "", "jd_text": JD_OK}
            with factory() as session:
                job, outcome = ingest(session, item, root / "jobs")
                self.assertEqual((outcome, job.status), ("new", "new"))
                self.assertTrue(Path(job.jd_path).is_file())
                _, outcome = ingest(session, item, root / "jobs")
                self.assertEqual(outcome, "seen")
                _, outcome = ingest(session, {**item, "jd_text": JD_OK + "Updated line.\n"}, root / "jobs")
                self.assertEqual(outcome, "updated")
                twin, outcome = ingest(session, {**item, "external_id": "2", "jd_text": JD_OK + "Updated line.\n"},
                                       root / "jobs")
                self.assertEqual((outcome, twin.status, twin.repost_of), ("repost", "screened_out", job.id))
                no_jd, outcome = ingest(session, {**item, "external_id": "3", "jd_text": ""}, root / "jobs")
                self.assertEqual((outcome, no_jd.status), ("no_jd", "no_jd"))
                _, outcome = ingest(session, {**item, "external_id": "3", "jd_text": "A different ad.\n" * 3},
                                    root / "jobs")
                self.assertEqual(outcome, "new")
                session.commit()
            engine.dispose()


def fake_collector_run(root, rows):
    """Write what the SEEK collector would leave behind and return a runner."""
    collector = root / "collector"
    collector.mkdir(parents=True, exist_ok=True)
    (collector / "collect_seek.py").write_text("# placeholder\n", encoding="utf-8")
    run = collector / "seek_output" / "runs" / "20260919T000000Z-abcd1234"
    (run / "jd").mkdir(parents=True, exist_ok=True)
    for row in rows:
        if row.get("description"):
            (run / "jd" / f"{row['job_id']}.txt").write_text(row["description"], encoding="utf-8")
            row["jd_file"] = f"jd/{row['job_id']}.txt"
    (run / "jobs.json").write_text(json.dumps(rows), encoding="utf-8")
    (run / "run_report.json").write_text(json.dumps({"unique_jobs": len(rows), "full_descriptions": 3,
                                                     "errors": [], "search_truncated": False}), encoding="utf-8")
    (collector / "seek_output" / "latest.json").write_text(json.dumps(
        {"run_id": run.name, "run_directory": f"runs/{run.name}"}), encoding="utf-8")
    calls = []

    def runner(collector_dir, max_pages, max_details, options=()):
        calls.append((max_pages, max_details))
        runner.options.append(list(options))
        return 0, "", ""
    runner.calls = calls
    runner.options = []
    return runner


ROWS = [
    {"job_id": "101", "title": "Junior Software Developer", "company": "Acme", "url": "https://x/101",
     "location": "Perth WA", "work_type": "Full time", "description": JD_OK},
    {"job_id": "102", "title": "2027 Graduate Program", "company": "BigCo", "url": "https://x/102",
     "location": "Perth WA", "work_type": "Full time", "description": JD_CITIZEN},
    {"job_id": "103", "title": "Senior Platform Engineer", "company": "Acme", "url": "https://x/103",
     "location": "Perth WA", "work_type": "Full time", "description": JD_OK + "Lead the team.\n"},
    {"job_id": "104", "title": "Junior Analyst", "company": "Gov", "url": "https://x/104",
     "location": "Perth WA", "work_type": "Full time", "description": ""},
]


class TickTests(unittest.TestCase):
    def test_tick_screens_queues_and_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            runner = fake_collector_run(root, [dict(r) for r in ROWS])
            (root / "jobs" / "inbox").mkdir(parents=True)
            # A different ad from the SEEK ones: an identical text would (correctly) count as a repost.
            (root / "jobs" / "inbox" / "handwritten.txt").write_text(
                JD_OK.replace("Junior Software Developer", "Junior Data Engineer"), encoding="utf-8")
            def respond(jd_text):
                if "New Zealand Citizen" in jd_text:
                    return {"verdict": "citizen_or_pr", "quote": "are either Australian or New Zealand Citizen",
                            "reason": "公民"}
                return {"verdict": "no_sponsorship_ok", "quote": "no visa sponsorship is available", "reason": "ok"}
            llm = FakeLLM(respond)
            stage_runner = FakeRunner(root / "outputs")
            app = create_app(settings, runner=stage_runner,
                             autopilot_kwargs={"seek_runner": runner, "llm_factory": lambda: llm,
                                               "launch": lambda path: None})
            app.state.worker.pipeline.model_online = lambda: (True, "fake")
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/autopilot/status").json()["running"], False)
                report = app.state.autopilot.tick(trigger="test", max_pages=2, max_details=7)
                self.assertEqual(runner.calls, [(2, 7)])
                self.assertEqual(report["collected"], 4)          # 3 complete SEEK JDs + 1 inbox
                self.assertEqual(report["new"], 4)                # 101, 102, 103 and the inbox JD
                self.assertEqual(report["no_jd"], 0)              # title-only 104 is not admitted
                titles = lambda key: sorted(b["title"] for b in report[key])  # noqa: E731
                self.assertEqual(titles("screened_out"), ["2027 Graduate Program", "Senior Platform Engineer"])
                self.assertEqual(len(report["queued"]), 2)        # 101 and the inbox JD
                self.assertEqual(report["flagged"], [])
                self.assertEqual(llm.calls, 3)                    # the senior title never reached the model
                graduate = next(b for b in report["screened_out"] if b["title"].startswith("2027"))
                self.assertIn("Citizen", graduate["quote"])
                self.assertEqual(client.get("/api/autopilot/reports").json()[0]["id"], report["id"])
                listed = client.get("/api/tasks")
                self.assertEqual(listed.status_code, 200, listed.text)   # tick rows never masquerade as tasks
                self.assertTrue(all(t["kind"] != "tick" for t in listed.json()))
                self.assertEqual(client.get(f"/api/tasks/{report['id']}").status_code, 404)
                # the queued jobs ran through the fake stages
                for task in list(app.state.worker.tasks.values()):
                    done = wait_for(client, task.id)
                    self.assertEqual(done["status"], "done", done["error"])
                statuses = {j["external_id"]: j["status"] for j in client.get("/api/jobs?limit=50").json()}
                self.assertEqual(statuses["101"], "done")
                self.assertEqual(statuses["102"], "screened_out")
                self.assertEqual(statuses["103"], "screened_out")
                self.assertNotIn("104", statuses)
                self.assertEqual(statuses["handwritten"], "done")
                detail = client.get(f"/api/jobs/{[j for j in client.get('/api/jobs').json() if j['external_id']=='102'][0]['id']}").json()
                self.assertEqual(detail["screening"]["model_verdict"], "citizen_or_pr")
                # a second tick finds nothing new and queues nothing
                again = app.state.autopilot.tick(trigger="test")
                self.assertEqual((again["new"], again["queued"], again["flagged"]), (0, [], []))
                self.assertEqual(again["seen"], 4)

    def test_offline_model_holds_jobs_and_next_tick_screens_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            runner = fake_collector_run(root, [dict(ROWS[0])])
            llm = FakeLLM([{"verdict": "unspecified", "quote": "", "reason": "x"}])
            online = {"value": False}
            app = create_app(settings, runner=FakeRunner(root / "outputs"),
                             autopilot_kwargs={"seek_runner": runner, "llm_factory": lambda: llm,
                                               "launch": lambda path: None})
            app.state.worker.pipeline.model_online = lambda: (online["value"], "x")
            with TestClient(app) as client:
                report = app.state.autopilot.tick(trigger="test")
                self.assertFalse(report["model_online"])
                self.assertEqual(len(report["held"]), 1)
                self.assertEqual(client.get("/api/jobs").json()[0]["status"], "held")
                online["value"] = True
                report = app.state.autopilot.tick(trigger="test")
                self.assertEqual(len(report["queued"]), 1)
                wait_for(client, list(app.state.worker.tasks)[0])

    def test_tick_endpoint_and_seek_runner_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            fake_collector_run(root, [])

            def failing(collector_dir, max_pages, max_details, options=()):
                return 1, "", "HTTP 403"
            app = create_app(settings, runner=FakeRunner(root / "outputs"),
                             autopilot_kwargs={"seek_runner": failing, "llm_factory": lambda: FakeLLM([]),
                                               "launch": lambda path: None})
            app.state.worker.pipeline.model_online = lambda: (True, "x")
            with TestClient(app) as client:
                self.assertEqual(client.post("/api/autopilot/tick", json={"sources": ["inbox", "seek"]}).status_code, 202)
                deadline = time.time() + 5
                while (app.state.autopilot.running or app.state.autopilot.last_report is None) and time.time() < deadline:
                    time.sleep(0.05)
                report = app.state.autopilot.last_report
                self.assertIn("HTTP 403", report["sources"]["seek"]["error"])
                self.assertEqual(report["collected"], 0)
                self.assertEqual(client.get("/api/autopilot/reports").json()[0]["errors"][0][:8], "SEEK 采集：")


class SourceTests(unittest.TestCase):
    def test_inbox_and_seek_loaders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            self.assertEqual(inbox.collect(settings), [])
            (root / "jobs" / "inbox").mkdir(parents=True)
            (root / "jobs" / "inbox" / "a.txt").write_text("﻿Data Analyst\nBody\n", encoding="utf-8")
            (root / "jobs" / "inbox" / "ignore.pdf").write_bytes(b"%PDF")
            items = inbox.collect(settings)
            self.assertEqual([(i["external_id"], i["title"]) for i in items], [("a", "Data Analyst")])
            runner = fake_collector_run(root, [dict(ROWS[0]), dict(ROWS[3])])
            items, info = seek.collect(settings, runner=runner)
            self.assertEqual([i["external_id"] for i in items], ["101"])
            self.assertTrue(items[0]["jd_text"].startswith("Junior Software Developer"))
            self.assertEqual(info["discarded_no_jd"], 1)
            self.assertEqual(info["exit_code"], 0)
            with self.assertRaises(RuntimeError):
                seek.collect(Settings(_env_file=None, config_path=Path("c.json"), collector_dir=None))


class BareReportTests(unittest.TestCase):
    def test_a_tick_row_without_a_report_keeps_the_report_shape(self):
        from types import SimpleNamespace
        from datetime import datetime, timezone
        from backend.app.api.autopilot import _bare_report
        row = SimpleNamespace(id="abc12345", error="boom", created_at=datetime(2026, 9, 20, tzinfo=timezone.utc))
        report = _bare_report(row)
        for key in ("queued", "flagged", "screened_out", "held", "notes"):
            self.assertEqual(report[key], [])
        self.assertEqual(report["errors"], ["boom"])
        self.assertTrue(report["started_at"].startswith("2026-09-20"))


if __name__ == "__main__":
    unittest.main()
