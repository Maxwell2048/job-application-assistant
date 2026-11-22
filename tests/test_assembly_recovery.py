import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from local_llm import LLMError, ServiceError, validate_against_schema
import build_resume
from tailor_cv import BULLETS_SCHEMA
from test_build_resume import setup_dir


class AssemblyRecoveryTests(unittest.TestCase):
    def fixture(self, root):
        materials, resume, suggestions = setup_dir(root)
        resume["text"] = "ALEX SAMPLE\nPerth, WA\n▐ EDUCATION\nExample University\nMaster of IT\n▐ TECHNICAL SKILLS\nPython and SQL\n▐ SELF-ASSESSMENT\nUnverified self praise"
        suggestions["projects"][0]["title"] = "Sample Mapping Project"
        return materials, resume, suggestions

    def test_bad_model_structure_recovers_only_existing_text_and_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            materials, resume, suggestions = self.fixture(Path(tmp))
            llm = Mock(prompt_char_budget=100000, estimate_context=lambda *a: 100)
            llm.generate_json.side_effect = LLMError("bad JSON", error_type="structure")
            notes = []
            result = build_resume.build(llm, suggestions, materials, resume, "JD", notes=notes)
            self.assertEqual(result[0]["lines"][0]["text"], "ALEX SAMPLE")
            self.assertTrue(notes)
            lines = [line for section in result for line in section["lines"]]
            self.assertTrue(any(line["text"] == "Annotated 12 sample maps in LabelMe format" for line in lines))
            self.assertFalse(any("self praise" in line["text"] for line in lines))
            self.assertTrue(all(line["basis"] for line in lines))

    def test_service_failures_are_not_hidden_by_layout_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            materials, resume, suggestions = self.fixture(Path(tmp))
            llm = Mock(prompt_char_budget=100000, estimate_context=lambda *a: 100)
            llm.generate_json.side_effect = ServiceError("offline")
            with self.assertRaises(ServiceError):
                build_resume.build(llm, suggestions, materials, resume, "JD")

    def test_unrecognised_resume_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            materials, resume, suggestions = setup_dir(Path(tmp))
            refs = build_resume.assembly_references(suggestions, resume)
            with self.assertRaises(LLMError):
                build_resume.assemble_verified_references(suggestions, resume, refs)

    def test_optional_note_does_not_make_bullet_facts_optional(self):
        data = {"title": "Sample Project", "bullets": [{"text": "Implemented a classifier.", "basis": [{"material_id": "M001", "quote": "classifier"}], "requirement_ids": []}]}
        self.assertEqual(validate_against_schema(data, BULLETS_SCHEMA), [])
        del data["bullets"][0]["basis"]
        self.assertTrue(validate_against_schema(data, BULLETS_SCHEMA))

    def test_flat_education_dates_stay_with_the_correct_school(self):
        import export_docx
        with tempfile.TemporaryDirectory() as tmp:
            materials, resume, suggestions = self.fixture(Path(tmp))
            resume["text"] = resume["text"].replace("Example University\nMaster of IT", "2024.02 - 2026.06\nExample University\nMaster of IT\n2020.09 - 2024.06\nOther University\nBachelor of Science")
            refs = build_resume.assembly_references(suggestions, resume)
            data = build_resume.assemble_verified_references(suggestions, resume, refs)
            build_resume.expand_refs(data, refs)
            education = next(s for s in data["sections"] if s["title"] == "Education")
            parsed = export_docx.parse_resume(["# ALEX SAMPLE", "## Education"] + [line["text"] for line in education["lines"]])
            section = next(s for s in parsed["sections"] if s["title"] == "Education")
            records, _ = export_docx._education_records(section)
            self.assertEqual([(r["school"], r["dates"]) for r in records], [("Example University", "2024.02 - 2026.06"), ("Other University", "2020.09 - 2024.06")])
