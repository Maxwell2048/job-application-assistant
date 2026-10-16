import unittest
from backend.app.core.config import Settings
from backend.app.services.screen import rule_screen, fit_decision
from backend.app.services.sources.seek import collector_options
from tools.seek_collector.search_rules import excluded_title

class SearchCoverageTests(unittest.TestCase):
    def screen(self, title="IT Support Officer", jd="Office based role.", location="Perth WA"):
        return rule_screen(dict(title=title, jd_text=jd, location=location, work_type="Full time"), Settings())

    def test_mixed_and_junior_titles_are_retained(self):
        for title in ["Senior Data Analyst | Data Analyst", "Senior Engineer / Software Engineer", "Junior Account Manager", "Assistant Project Manager"]:
            self.assertFalse(self.screen(title=title)["exclude"], title)
        self.assertTrue(self.screen(title="Senior Software Engineer")["exclude"])

    def test_fifo_negation_and_client_mentions(self):
        for text in ["No FIFO required.", "FIFO is not required.", "Support our FIFO clients."]:
            self.assertFalse(self.screen(jd=text)["exclude"], text)
        self.assertFalse(self.screen(title="IT Support - No FIFO")["exclude"])
        self.assertTrue(self.screen(jd="This is a FIFO role with a 2:1 roster.")["exclude"])
        self.assertTrue(self.screen(title="FIFO IT Support")["exclude"])

    def test_perth_suburbs_and_uncertain_wa(self):
        for place in ["Canning Vale WA", "Joondalup", "Fremantle WA", "Unlisted suburb WA"]:
            self.assertFalse(self.screen(location=place)["exclude"], place)
        self.assertTrue(self.screen(location="Sydney NSW")["exclude"])
        self.assertFalse(self.screen(location="All Australia", jd="Work from home across Australia.")["exclude"])

    def test_remote_preference_reaches_collector(self):
        self.assertIn("--include-remote", collector_options(Settings(), "focused"))
        self.assertIn("--no-include-remote", collector_options(Settings(allow_remote=False), "broad"))

    def test_mixed_title_fit_is_flagged_not_skipped(self):
        fit=dict(level="senior", role_family="data_ai", min_years=0, years_quoted=False, years_mandatory=False, missing_stack=[], degree="unspecified", constraints=[])
        rules=dict(junior=False, senior="seeking a senior analyst", years=0, constraints=[])
        skips, flags=fit_decision(fit, "Senior Data Analyst | Data Analyst", rules, Settings())
        self.assertEqual(skips, [])
        self.assertTrue(flags)


class ManualSkipIngestTests(unittest.TestCase):
    def test_first_jd_does_not_restore_a_manually_skipped_title_only_job(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from backend.app.db.models import Base, Job
        from backend.app.services.ledger import ingest
        engine=create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with TemporaryDirectory() as tmp, Session(engine) as session:
            job=Job(source="seek",external_id="123",title="Developer",status="screened_out",note="手动跳过")
            session.add(job);session.commit()
            job, _=ingest(session,dict(source="seek",external_id="123",title="Developer",jd_text="Build software applications."),Path(tmp))
            self.assertEqual(job.status,"screened_out")
            self.assertTrue(job.jd_sha256)
        engine.dispose()


class ItScopeTests(unittest.TestCase):
    def test_level_words_and_generic_jobs_are_not_technical_evidence(self):
        from tools.seek_collector.search_rules import it_evidence
        for title in ["Graduate Accountant", "Junior Lawyer", "Entry Level Sales Consultant", "Training Administration Assistant", "Trainee CNC Plasma Programmer", "Graduate Environmental Consultant", "Power System Protection Specialist"]:
            self.assertFalse(it_evidence(title),title)
        for title in ["Graduate Software Developer", "Healthcare Data Analyst", "IT Support Officer", "Service Desk Analyst", "Junior Test Analyst"]:
            self.assertTrue(it_evidence(title),title)

    def test_non_it_graduate_no_longer_bypasses_fit_skip(self):
        fit=dict(level="graduate",role_family="non_technical",min_years=0,years_quoted=False,years_mandatory=False,missing_stack=[],degree="unspecified",constraints=[])
        skips, _=fit_decision(fit,"Graduate Accountant",dict(junior=True,senior="",years=0,constraints=[]),Settings())
        self.assertTrue(skips)
