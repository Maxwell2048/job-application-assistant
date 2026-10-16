import unittest
from backend.app.services.location_rules import employer_body, remote_evidence
from backend.app.services.screen import rule_screen
from backend.app.core.config import Settings

class LocationTests(unittest.TestCase):
    def test_collector_and_company_remote_are_not_offers(self):
        text="[SEEK listing metadata — separate from employer description]\nLocation is the listing label, not a confirmed attendance or remote-work requirement.\n[Employer job description]\nTest software applications."
        self.assertEqual(employer_body(text),"Test software applications.")
        self.assertEqual(remote_evidence(text,"Sydney NSW")["status"],"unconfirmed")
        for text in ["Support medical professionals in rural and remote areas.","Travel to remote sites.","Flexible work options include hybrid work."]:
            self.assertNotEqual(remote_evidence(text,"Sydney NSW")["status"],"confirmed")

    def test_outside_office_restrictions_override_remote(self):
        for text in ["Fully remote role. Must be based in Sydney.","This role requires 3 days per week in the Brisbane office.","Hybrid, 3 days a week in the Melbourne CBD.","Work remotely from Sydney. 100% remote work.","Remote working: work outside your assigned home location for up to 30 days every year.","No remote work. Work from anywhere in Australia."]:
            self.assertNotEqual(remote_evidence(text,"Sydney NSW")["status"],"confirmed",text)

    def test_explicit_perth_or_nationwide_remote_allowed(self):
        for text in ["Fully remote role, work from anywhere in Australia.","Work from home across Australia.","100% remote. Candidates can work anywhere in Australia.","Our company is based in Sydney. Fully remote role, work from anywhere in Australia."]:
            self.assertEqual(remote_evidence(text,"Sydney NSW")["status"],"confirmed",text)
        self.assertEqual(remote_evidence("100% remote work.","All Australia")["status"],"confirmed")
        self.assertNotEqual(remote_evidence("Fully remote role.","Sydney NSW")["status"],"confirmed")

    def test_rule_records_employer_quote_and_excludes_unconfirmed(self):
        job=dict(title="Software Engineer",location="Sydney NSW",work_type="Full time",jd_text="Travel to remote sites.")
        result=rule_screen(job,Settings())
        self.assertTrue(result["exclude"])
        self.assertEqual(result["location_check"]["status"],"unconfirmed")
        job["jd_text"]="Fully remote, work from anywhere in Australia."
        result=rule_screen(job,Settings())
        self.assertFalse(result["exclude"])
        self.assertIn("anywhere in Australia",result["location_check"]["quote"])
