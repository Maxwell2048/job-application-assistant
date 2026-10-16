"""Fit gate tests: the rule helpers, the program-side decision, the gate
against a fake model, the tick with the gate on, and the evidence coverage
the board sorts by. No network, no subprocesses."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.app.core.config import Settings  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.app.services import screen  # noqa: E402
from backend.app.services.runs import condition_coverage  # noqa: E402
from backend.tests.test_api import FakeRunner, wait_for  # noqa: E402
from backend.tests.test_autopilot import FIT_PLAIN, FakeLLM, fake_collector_run, settings_for  # noqa: E402

KNOWN = ["Python", "SQL", "JavaScript", "React", "Flask", "REST APIs", "Git", "Docker"]

JD_DEV = ("Junior Software Developer\n\nWe are looking for a junior developer to join our Perth team.\n"
          "Requirements\nStrong Python and SQL skills.\nExperience with C# and .NET is required.\n")
JD_SENIOR_YEARS = ("Network Engineer\n\nEssential criteria\nCisco routing and switching\n"
                   "5 years' experience in network administration\nWhat we offer\nA great team.\n")
JD_IDEAL_YEARS = "Network Engineer\n\nIdeally 5 years experience in network administration.\nCisco routing.\n"
JD_FINANCE = "Finance Business Partner\n\nPartner with the CFO on budgets and forecasting.\nCPA qualified.\n"
JD_SUPPORT = ("Service Desk Analyst\n\nProvide first-level support. You will join an on-call roster once a month.\n"
              "A current driver's licence is essential.\n")


def fit_answer(**changes):
    return {**FIT_PLAIN, **changes}


class RuleHelperTests(unittest.TestCase):
    def test_mandatory_years(self):
        rule = screen.mandatory_years_by_rule
        self.assertEqual(rule("Minimum 3 years of experience in .NET development.", 2)[0], 3)
        self.assertEqual(rule(JD_SENIOR_YEARS, 2)[0], 5)                         # mandatory by its heading
        self.assertEqual(rule("Desirable\n5 years experience with Azure\n", 2)[0], 0)
        self.assertEqual(rule(JD_IDEAL_YEARS, 2)[0], 0)                          # "ideally" is a concession
        self.assertEqual(rule("At least 3 years experience or a recent graduate with strong projects.", 2)[0], 0)
        self.assertEqual(rule("With over 70 years of experience, we must deliver.", 2)[0], 0)   # company history
        self.assertEqual(rule("0-2 years of experience required.", 2)[0], 0)
        self.assertEqual(rule("3-5 years experience is required.", 2)[0], 3)     # the lower bound decides
        self.assertEqual(rule("3-5 years experience is required.", 3)[0], 0)
        # a plain short bullet inside the list does not end the "Essential" section
        self.assertEqual(rule("Essential\nPython and SQL\n4 years experience in support\n", 2)[0], 4)
        # a new section heading does
        self.assertEqual(rule("Essential\nPython\nAbout you\n4 years experience in support\n", 2)[0], 0)

    def test_senior_wording_about_the_hire(self):
        rule = screen.senior_by_rule
        self.assertIn("senior software engineer", rule("As a senior software engineer, you will be responsible for:"))
        self.assertTrue(rule("Please note that this position is for an experienced IT staff."))
        self.assertTrue(rule("We are looking for an experienced and motivated developer."))
        self.assertEqual(rule("You will work alongside senior engineers and report to the Lead."), "")
        self.assertEqual(rule("Prepare reports for senior stakeholders."), "")

    def test_junior_and_constraints(self):
        self.assertTrue(screen.junior_by_rule("Junior Developer", ""))
        self.assertTrue(screen.junior_by_rule("Developer", "This role would suit a graduate keen to learn."))
        self.assertFalse(screen.junior_by_rule("Team Lead", "You will mentor junior developers."))
        found = screen.constraints_by_rule(JD_SUPPORT + "This is a 12 month contract.")
        self.assertEqual(sorted(found), ["driver_licence", "fixed_term", "shift_work"])
        self.assertIn("on-call roster", found["shift_work"])
        self.assertEqual(screen.constraints_by_rule("A permanent full-time role in Perth."), {})

    def test_known_and_missing_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile.md"
            profile.write_text("# 自述\n- 相关能力：Python, SQL，React、REST APIs。\n相关能力: Docker\n其他：C#\n",
                               encoding="utf-8")
            known = screen.known_stack(profile)
            self.assertEqual(known, ["Python", "SQL", "React", "REST APIs", "Docker"])
            self.assertEqual(screen.known_stack(Path(tmp) / "missing.md"), [])
        missing = screen.missing_stack(["React.js", "Python 3", "REST API", "C#", ".NET", "Node.js", "HTML", "CSS"],
                                       known + ["HTML5", "CSS3"])
        self.assertEqual(missing, ["C#", ".NET", "Node.js"])
        # using a product is not administering it
        office = ["Microsoft 365", "Windows"]
        self.assertEqual(screen.missing_stack(["Microsoft 365 applications", "Microsoft 365 admin center",
                                               "Windows 11", "Windows Server"], office),
                         ["Microsoft 365 admin center", "Windows Server"])


class FitGateTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(_env_file=None, config_path=Path("c.json"))

    def gate(self, jd, title, answer, **settings):
        llm = FakeLLM([], fit=[answer] if isinstance(answer, dict) else answer)
        return screen.fit_gate(jd, title, llm, self.settings.model_copy(update=settings), KNOWN)

    def test_plain_junior_role_runs_with_stack_hint(self):
        result = self.gate(JD_DEV, "Junior Software Developer", fit_answer(
            level="junior", level_quote="looking for a junior developer",
            required_stack=["Python", "SQL", "C#", ".NET", "Kubernetes"]))
        self.assertEqual(result["decision"], "run")
        self.assertEqual(result["required_stack"], ["Python", "SQL", "C#", ".NET"])   # Kubernetes is not in the JD
        self.assertEqual(result["missing_stack"], ["C#", ".NET"])
        self.assertEqual(result["flags"], ["必需技术不在个人技术栈：C#、.NET"])
        self.assertTrue(result["rules"]["junior"])

    def test_mandatory_years_skip_needs_rule_and_model(self):
        answer = fit_answer(level="mid", level_quote="5 years' experience in network administration", min_years=5,
                            years_mandatory=True, years_quote="5 years' experience in network administration",
                            role_family="infrastructure")
        result = self.gate(JD_SENIOR_YEARS, "Network Engineer", answer)
        self.assertEqual(result["decision"], "skip")
        self.assertIn("5 年", result["skips"][0])
        # the model calls "ideally 5 years" mandatory; the rule does not agree: a flag, not a skip
        answer = {**answer, "level_quote": "Ideally 5 years experience", "years_quote": "Ideally 5 years experience"}
        result = self.gate(JD_IDEAL_YEARS, "Network Engineer", answer)
        self.assertEqual(result["decision"], "run")
        self.assertTrue(any("规则未确认" in f for f in result["flags"]))
        self.assertIn("岗位类型：系统与网络", result["flags"])

    def test_senior_level_skip_needs_the_jd_to_say_so(self):
        jd = "Full Stack Developer\n\nAs a senior software engineer, you will be responsible for the following:\n"
        answer = fit_answer(level="senior", level_quote="As a senior software engineer")
        result = self.gate(jd, "Full Stack Developer", answer)
        self.assertEqual(result["decision"], "skip")
        self.assertIn("高级岗位", result["skips"][0])
        self.assertEqual(self.gate(jd, "Full Stack Developer", answer, fit_skip_levels=[])["decision"], "run")
        # the model alone, or junior wording anywhere in the JD, is a flag
        plain = "Support Technician\n\nYou've been grinding remote tickets for years.\n"
        result = self.gate(plain, "Support Technician", fit_answer(level="mid", level_quote="grinding remote tickets"))
        self.assertEqual((result["decision"], result["flags"][0]), ("run", "模型判为中级岗位"))
        mixed = jd + "Graduates are welcome to apply.\n"
        self.assertEqual(self.gate(mixed, "Full Stack Developer", answer)["decision"], "run")

    def test_years_without_a_number_in_the_quote(self):
        jd = "Database Administrator\n\nSubstantial experience in planning database changes is essential.\n"
        result = self.gate(jd, "Database Administrator", fit_answer(
            role_family="infrastructure", min_years=3, years_mandatory=True,
            years_quote="Substantial experience in planning database changes"))
        self.assertEqual(result["decision"], "run")
        self.assertIn("原句没有具体年限", result["flags"][0])

    def test_non_technical_skip_needs_a_non_technical_title(self):
        answer = fit_answer(role_family="non_technical")
        self.assertEqual(self.gate(JD_FINANCE, "Finance Business Partner", answer)["decision"], "skip")
        result = self.gate(JD_FINANCE, "IT Sales Engineer", answer)
        self.assertEqual(result["decision"], "run")
        self.assertTrue(any("标题像技术岗位" in f for f in result["flags"]))

    def test_support_role_and_constraints_are_flagged_unless_configured(self):
        answer = fit_answer(role_family="it_support", constraints=[
            {"kind": "shift_work", "quote": "You will join an on-call roster once a month."},
            {"kind": "driver_licence", "quote": "A current driver's licence is essential."}])
        result = self.gate(JD_SUPPORT, "Service Desk Analyst", answer)
        self.assertEqual(result["decision"], "run")
        self.assertEqual(result["flags"], ["岗位类型：IT 支持", "驾照或自备车辆", "轮班或 on-call"])
        skipped = self.gate(JD_SUPPORT, "Service Desk Analyst", answer, fit_skip_constraints=["shift_work"])
        self.assertEqual((skipped["decision"], skipped["skips"]), ("skip", ["轮班或 on-call"]))
        # a constraint only the rule sees never skips
        rule_only = self.gate(JD_SUPPORT, "Service Desk Analyst", fit_answer(role_family="it_support"),
                              fit_skip_constraints=["shift_work"])
        self.assertEqual(rule_only["decision"], "run")
        self.assertIn("轮班或 on-call", rule_only["flags"])

    def test_invented_quotes_are_retried_then_the_job_runs_flagged(self):
        bad = fit_answer(level="junior", level_quote="Entry-level applicants only")
        result = self.gate(JD_SUPPORT, "Service Desk Analyst", [bad, bad, bad])
        self.assertEqual(result["decision"], "run_flagged")
        self.assertIn("匹配度预筛失败", result["flags"][0])
        self.assertIn("轮班或 on-call", result["flags"])            # the rule findings are kept
        fixed = self.gate(JD_SUPPORT, "Service Desk Analyst", [bad, fit_answer(role_family="it_support")])
        self.assertEqual(fixed["decision"], "run")


ROWS = [
    {"job_id": "201", "title": "Junior Software Developer", "company": "Acme", "url": "https://x/201",
     "location": "Perth WA", "work_type": "Full time", "description": JD_DEV * 2},
    {"job_id": "202", "title": "Finance Business Partner", "company": "Money", "url": "https://x/202",
     "location": "Perth WA", "work_type": "Full time", "description": JD_FINANCE * 2},
    {"job_id": "203", "title": "Service Desk Analyst", "company": "Help", "url": "https://x/203",
     "location": "Perth WA", "work_type": "Full time", "description": JD_SUPPORT * 2},
]


class FitTickTests(unittest.TestCase):
    def test_tick_skips_by_fit_and_notes_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            (root / "profile.md").write_text("相关能力：" + ", ".join(KNOWN) + "\n", encoding="utf-8")
            runner = fake_collector_run(root, [dict(r) for r in ROWS])

            def fit(jd_text):
                if "Finance" in jd_text:
                    return fit_answer(role_family="non_technical")
                if "Service Desk" in jd_text:
                    return fit_answer(role_family="it_support")
                return fit_answer(required_stack=["Python", "C#"])
            llm = FakeLLM(lambda jd: {"verdict": "unspecified", "quote": "", "reason": "没提到"}, fit=fit)
            app = create_app(settings, runner=FakeRunner(root / "outputs"),
                             autopilot_kwargs={"seek_runner": runner, "llm_factory": lambda: llm,
                                               "launch": lambda path: None})
            app.state.worker.pipeline.model_online = lambda: (True, "fake")
            with TestClient(app) as client:
                report = app.state.autopilot.tick(trigger="test", sources=("seek",))
                self.assertEqual((llm.calls, llm.fit_calls), (3, 3))
                self.assertEqual([b["title"] for b in report["screened_out"]], ["Finance Business Partner"])
                self.assertIn("非技术岗位", report["screened_out"][0]["reason"])
                self.assertEqual(sorted(b["title"] for b in report["queued"]),
                                 ["Junior Software Developer", "Service Desk Analyst"])
                for task in list(app.state.worker.tasks.values()):
                    wait_for(client, task.id)
                jobs = {j["external_id"]: j for j in client.get("/api/jobs").json()}
                self.assertEqual(jobs["202"]["status"], "screened_out")
                self.assertEqual(jobs["202"]["screening"]["decision"], "skip")
                self.assertEqual(jobs["202"]["screening"]["model_verdict"], "unspecified")
                self.assertEqual(jobs["202"]["screening"]["rule_result"]["fit"]["skips"], ["岗位类型：非技术岗位"])
                self.assertIn("必需技术不在个人技术栈：C#", jobs["201"]["note"])
                self.assertIn("预筛提示：岗位类型：IT 支持", jobs["203"]["note"])
                self.assertEqual(jobs["203"]["screening"]["decision"], "run")

    def test_fit_gate_can_be_switched_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root).model_copy(update={"fit_screen_enabled": False})
            runner = fake_collector_run(root, [dict(ROWS[1])])
            llm = FakeLLM(lambda jd: {"verdict": "unspecified", "quote": "", "reason": "x"})
            app = create_app(settings, runner=FakeRunner(root / "outputs"),
                             autopilot_kwargs={"seek_runner": runner, "llm_factory": lambda: llm,
                                               "launch": lambda path: None})
            app.state.worker.pipeline.model_online = lambda: (True, "fake")
            with TestClient(app) as client:
                report = app.state.autopilot.tick(trigger="test", sources=("seek",))
                self.assertEqual((llm.fit_calls, len(report["queued"])), (0, 1))
                wait_for(client, list(app.state.worker.tasks)[0])
                self.assertNotIn("fit", client.get("/api/jobs").json()[0]["screening"]["rule_result"])


class CoverageTests(unittest.TestCase):
    def test_conditions_exclude_responsibilities_preferred_and_location(self):
        requirements = [
            {"requirement_id": "R1", "category": "technical_skill", "importance": "required", "text": "Python"},
            {"requirement_id": "R2", "category": "technical_skill", "importance": "unspecified", "text": "SQL"},
            {"requirement_id": "R3", "category": "responsibility", "importance": "unspecified", "text": "Build APIs"},
            {"requirement_id": "R4", "category": "technical_skill", "importance": "preferred", "text": "Azure"},
            {"requirement_id": "R5", "category": "location", "importance": "required", "text": "Perth"},
            {"requirement_id": "R6", "category": "certification", "importance": "required", "text": "Driver licence"},
            {"requirement_id": "R7", "category": "experience", "importance": "required",
             "text": "0-2 years of software development experience"},
            {"requirement_id": "R8", "category": "experience", "importance": "required", "text": "Network experience",
             "qualifiers": [{"type": "years", "value": "5+", "source_quote": "5+ years"}]},
        ]
        verdicts = {"R1": "direct", "R2": "related", "R3": "insufficient", "R4": "insufficient", "R5": "direct",
                    "R6": "insufficient", "R7": "related", "R8": "related"}
        matches = [{"requirement_id": rid, "verdict": v, "processing_status": "ok"} for rid, v in verdicts.items()]
        matches.append({"requirement_id": "R9", "verdict": None, "processing_status": "error"})
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            self.assertEqual(condition_coverage(run), {})
            (run / "requirements.json").write_text(json.dumps({"requirements": requirements}), encoding="utf-8")
            (run / "matches.json").write_text(json.dumps({"matches": matches}), encoding="utf-8")
            coverage = condition_coverage(run)
        # R1, R2, R6, R7, R8 and the errored R9 are conditions; 0-2 years is not a hard gap, 5+ years is
        self.assertEqual(coverage, {"total": 6, "direct": 1, "related": 3, "insufficient": 1, "error": 1,
                                    "hard_gaps": 2})


if __name__ == "__main__":
    unittest.main()
