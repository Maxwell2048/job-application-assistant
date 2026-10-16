import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import export_docx
from docx import Document
from docx.oxml.ns import qn

MD = """# ALEX SAMPLE
IT Graduate · Data Analysis
📍 Perth, WA
✉ someone@example.com
📞 +61 400 000 000

## SUMMARY
Master of IT graduate with full working rights.

## EDUCATION

### University of Western Australia
Master of IT · AI Track | 2024.02 – 2026.06
Coursework: Data Warehousing · RDBMS

## KEY PROJECTS

### Finance Planner | Flask Web App
- Model: Built the login pages.
- Delivered the forum module.

## ADDITIONAL PROJECTS

### GeoMind | Object Detection
- Annotated 57 maps.

### CyberWin | Requirements
- Recorded interviews.

## TECHNICAL SKILLS
Languages: Python · SQL
Data & DB: PostgreSQL

## SELF-ASSESSMENT
Profile: Strong self-learner.

---

## 来源附录（发送前删除本节及以下）

- 1.1 “ALEX SAMPLE” ← [简历底稿] ...
"""


def all_paragraphs(doc):
    """Every paragraph in body and (nested) tables, in document order."""
    found = []

    def walk_cell(cell):
        for para in cell.paragraphs:
            found.append(para)
        for table in cell.tables:
            for row in table.rows:
                for c in row.cells:
                    walk_cell(c)
    for para in doc.paragraphs:
        found.append(para)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                walk_cell(cell)
    return found


class ParseTests(unittest.TestCase):
    def test_resume_body_and_parse(self):
        lines = export_docx.resume_body(MD)
        self.assertFalse(any("来源附录" in l for l in lines))
        parsed = export_docx.parse_resume(lines)
        self.assertEqual(parsed["name"], "ALEX SAMPLE")
        self.assertEqual(parsed["title"], "IT Graduate · Data Analysis")
        self.assertEqual(len(parsed["contact"]), 3)
        self.assertEqual([s["title"] for s in parsed["sections"]],
                         ["SUMMARY", "EDUCATION", "KEY PROJECTS", "ADDITIONAL PROJECTS", "TECHNICAL SKILLS", "SELF-ASSESSMENT"])
        edu = parsed["sections"][1]["blocks"][0]
        self.assertEqual(edu["heading"], "University of Western Australia")
        self.assertEqual(export_docx._split_degree_line(edu["lines"][0]["text"]),
                         ("Master of IT · AI Track", "2024.02 – 2026.06"))
        proj = parsed["sections"][2]["blocks"][0]
        self.assertEqual([l["kind"] for l in proj["lines"]], ["bullet", "bullet"])


class ExportTests(unittest.TestCase):
    def test_education_keeps_parenthesized_dates_with_each_school(self):
        md = """# Test Applicant
## EDUCATION
### University of Western Australia
Master of IT · AI Track (2024.02 – 2026.06)
Coursework: Machine Learning
### Wuhan Polytechnic University, China
B.Eng. Food Science (2020.09 – 2024.06)
"""
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "resume.docx"
            export_docx.markdown_to_docx(md.splitlines(), output)
            doc = Document(output)
            self.assertEqual(doc.tables, [])
            parsed = export_docx.parse_resume(md.splitlines())
            records, notes = export_docx._education_records(parsed["sections"][0])
            self.assertEqual([(r["dates"], r["school"], r["degree"]) for r in records],
                [("2024.02 – 2026.06", "University of Western Australia", "Master of IT · AI Track"),
                 ("2020.09 – 2024.06", "Wuhan Polytechnic University, China", "B.Eng. Food Science")])
            self.assertEqual(records[0]["notes"], ["Coursework: Machine Learning"])
            text = "\n".join(p.text for p in doc.paragraphs)
            self.assertIn("University of Western Australia\t2024.02 – 2026.06", text)
            self.assertIn("Wuhan Polytechnic University, China\t2020.09 – 2024.06", text)
            self.assertIn("Coursework: Machine Learning", text)

    def test_degree_without_dates_stays_in_education_table(self):
        doc = Document()
        export_docx._education(doc, {"blocks": [{"heading": "Test University",
            "lines": [{"kind": "text", "text": "Master of IT (AI Track)"}]}]})
        self.assertEqual([c.text for c in doc.tables[0].rows[0].cells],
                         ["", "Test University", "Master of IT (AI Track)"])

    def test_export_uses_readable_consistent_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "resume_tailored.md").write_text(MD, encoding="utf-8")
            (run / "cv_suggestions.json").write_text(json.dumps({"cover_letter": [
                {"text": "Dear Hiring Manager,\n\nI am writing about the role."}]}), encoding="utf-8")
            (run / "run_meta.json").write_text(json.dumps({"input": {
                "jd_path": "D:\\x\\jobs\\20260912-0930-acme service_desk!.txt"}}), encoding="utf-8")
            outputs = export_docx.export_run(run, with_letter=True)
            # each run's files carry the person, the JD name and the run time
            self.assertRegex(outputs["resume"].name, r"^Alex_Sample_Resume_acme-service-desk_.+\.docx$")
            self.assertRegex(outputs["cover_letter"].name, r"^Alex_Sample_Cover_Letter_acme-service-desk_.+\.docx$")
            self.assertEqual(export_docx.latest_docx(run, "resume"), outputs["resume"])
            doc = Document(outputs["resume"])
            paras = all_paragraphs(doc)
            texts = [p.text for p in paras if p.text.strip()]
            name = next(p for p in paras if p.text == "ALEX SAMPLE")
            self.assertEqual(name.style.name, "Title")
            self.assertEqual(name.runs[0].font.size.pt, 22)
            self.assertEqual(str(name.runs[0].font.color.rgb), "000000")
            self.assertIn("Perth, WA  |  someone@example.com", texts)
            heading = next(p for p in paras if p.text == "EDUCATION")
            self.assertEqual(heading.style.name, "Heading 1")
            self.assertTrue(heading.paragraph_format.keep_with_next)
            self.assertEqual(doc.tables, [])
            self.assertTrue(any("2024.02 – 2026.06" in t for t in texts))
            self.assertIn("Master of IT · AI Track", texts)
            bullet = next(p for p in paras if p.text.startswith("Model: "))
            self.assertEqual(bullet.style.name, "List Bullet")
            self.assertEqual(bullet.runs[0].font.size.pt, 9.5)
            self.assertTrue(bullet.paragraph_format.keep_together)
            self.assertIn("Languages: Python · SQL", texts)
            # "Name | Type" becomes its own entry line plus a subtitle line
            self.assertIn("GeoMind", texts)
            self.assertIn("Object Detection", texts)
            self.assertIn("CyberWin", texts)
            self.assertIn("Requirements", texts)
            for style_name in ("Title", "Heading 1", "Heading 2"):
                ppr = doc.styles[style_name].element.pPr
                self.assertTrue(ppr is None or ppr.find(qn("w:pBdr")) is None)
            self.assertFalse(any("来源附录" in t for t in texts))
            # cover letter
            letter_texts = [p.text for p in Document(outputs["cover_letter"]).paragraphs if p.text.strip()]
            self.assertEqual(letter_texts[0], "ALEX SAMPLE")
            self.assertIn("I am writing about the role.", letter_texts)

    def test_file_names_are_english_and_lead_with_the_company(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "20260917-145916-63174a"
            run.mkdir()
            (run / "run_meta.json").write_text(json.dumps({"input": {
                "jd_path": "jobs/20260917-1459-highlights-重点内容.txt"}}), encoding="utf-8")
            # no identity yet: only the English part of the JD name is used
            self.assertEqual(export_docx.docx_name("resume", "ALEX SAMPLE", run),
                             "Alex_Sample_Resume_highlights_0917-1459.docx")
            (run / "job_identity.json").write_text(json.dumps({
                "company": "Acme Energy Pty Ltd", "job_title": "Graduate AI Engineer（人工智能）"}), encoding="utf-8")
            name = export_docx.docx_name("resume", "ALEX SAMPLE", run)
            self.assertEqual(name, "Alex_Sample_Resume_Acme-Energy_Graduate-AI-Engineer_0917-1459.docx")
            self.assertTrue(name.isascii())
            self.assertEqual(export_docx.docx_name("cover_letter", "ALEX SAMPLE", run, 2),
                             "Alex_Sample_Cover_Letter_Acme-Energy_Graduate-AI-Engineer_0917-1459-2.docx")
            # a label typed by the user wins; a label with no English letters falls back to the identity
            self.assertEqual(export_docx.docx_name("resume", "ALEX SAMPLE", run, label="Acme grad role"),
                             "Alex_Sample_Resume_Acme-grad-role_0917-1459.docx")
            self.assertIn("Acme-Energy", export_docx.docx_name("resume", "ALEX SAMPLE", run, label="某公司"))
            (run / "job_identity.json").write_text(json.dumps({"company": None, "job_title": "Data Analyst"}),
                                                   encoding="utf-8")
            self.assertEqual(export_docx.docx_name("resume", "ALEX SAMPLE", run),
                             "Alex_Sample_Resume_Data-Analyst_0917-1459.docx")
            bare = Path(tmp) / "20260919-101010-ffffff"  # nothing known: the time alone still separates runs
            bare.mkdir()
            self.assertEqual(export_docx.docx_name("resume", None, bare), "Resume_0919-1010.docx")
            (run / "resume_tailored.docx").write_bytes(b"PK")  # exported before the naming change
            self.assertEqual(export_docx.latest_docx(run, "resume").name, "resume_tailored.docx")
            self.assertIsNone(export_docx.latest_docx(run, "cover_letter"))

    def test_other_projects_flow_in_short_rows(self):
        blocks = [{"heading": f"Project {i}", "lines": [{"kind": "bullet", "text": "Did a thing."}]} for i in range(3)]
        doc = Document()
        table = export_docx._two_columns(doc, blocks, lambda cell, block: export_docx._project_block(
            cell, block, keep_heading=False))
        self.assertEqual(len(table.rows), 2)  # two projects per row, not one tall unbreakable row
        self.assertEqual([c.paragraphs[0].text for c in table.rows[0].cells], ["Project 0", "Project 1"])
        last = table.rows[1]  # the odd block spans both columns
        self.assertEqual(last.cells[0]._tc, last.cells[1]._tc)
        self.assertEqual(last.cells[0].paragraphs[0].text, "Project 2")
        for row in table.rows:
            self.assertIsNotNone(row._tr.trPr.find(qn("w:cantSplit")))
            for cell in row.cells:
                self.assertFalse(any(p.paragraph_format.keep_with_next for p in cell.paragraphs))

    def test_chinese_text_is_never_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "resume_tailored.md").write_text(MD.replace("### GeoMind", "### 示例项目：地图标注系统\n### GeoMind", 1),
                                                    encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                export_docx.export_run(run)
            self.assertIn("含中文", str(caught.exception))
            self.assertEqual(list(run.glob("*.docx")), [])
            (run / "resume_tailored.md").write_text(MD, encoding="utf-8")
            (run / "cv_suggestions.json").write_text(json.dumps({"cover_letter": [{"text": "我对这个职位很感兴趣。"}]}),
                                                     encoding="utf-8")
            with self.assertRaises(ValueError):
                export_docx.export_run(run, with_letter=True)

    def test_missing_resume_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                export_docx.export_run(tmp)


if __name__ == "__main__":
    unittest.main()


class ResumeLayoutRegressionTests(unittest.TestCase):
    def test_flat_education_groups_dates_school_degree_without_losing_coursework(self):
        md = "# ALEX\n## EDUCATION\n2024.02 – 2026.06\nTest University\nMaster of IT\n2020.09 – 2024.06\nOther University\nBachelor of Science\nCoursework: Machine Learning\n"
        parsed = export_docx.parse_resume(md.splitlines())
        records, notes = export_docx._education_records(parsed["sections"][0])
        self.assertEqual([(r["dates"],r["school"],r["degree"]) for r in records],
            [("2024.02 – 2026.06","Test University","Master of IT"),("2020.09 – 2024.06","Other University","Bachelor of Science")])
        self.assertEqual(notes, ["Coursework: Machine Learning"])

    def test_decorative_source_titles_do_not_create_empty_project_blocks(self):
        md = "# ALEX\n## OTHER PROJECTS\n▐ OTHER PROJECTS — DATA\n### Project A\n- One fact.\n### Project B\n- Another fact.\n"
        parsed = export_docx.parse_resume(md.splitlines())
        self.assertEqual([b["heading"] for b in parsed["sections"][0]["blocks"]], ["Project A","Project B"])
        self.assertEqual(parsed["sections"][0]["blocks"][0]["lines"][0]["text"], "One fact.")


class ColourAnnotationTests(unittest.TestCase):
    """The five-way colour annotation is decided by the program from the text alone."""

    def kinds(self, text):
        return [kind for _, kind in export_docx.annotate(text) if kind]

    def test_measurable_result(self):
        self.assertEqual(self.kinds("Cut p99 latency from 180ms to 62ms."), ["result"])
        self.assertEqual(self.kinds("Achieving 14/14 Recall@5 on a 22-case set."), ["result"])

    def test_technical_skill(self):
        self.assertEqual(self.kinds("Built it with PyTorch and Docker."), ["skill", "skill"])

    def test_leadership_and_career_growth(self):
        self.assertEqual(self.kinds("Mentored three new graduates."), ["lead"])
        self.assertEqual(self.kinds("Promoted to a senior engineering role."), ["growth"])

    def test_key_strength_is_shaded(self):
        self.assertEqual(self.kinds("Hands-on experience with end-to-end delivery."), ["strength"])

    def test_plain_line_is_not_coloured(self):
        self.assertEqual(self.kinds("Recorded the meeting minutes."), [])

    def test_annotation_never_changes_the_text(self):
        line = "Led the migration, cutting p99 latency from 180ms to 62ms, using PyTorch."
        self.assertEqual("".join(chunk for chunk, _ in export_docx.annotate(line)), line)

    def test_rendered_runs_carry_the_colours_and_keep_every_character(self):
        md = ("# ALEX SAMPLE\n## KEY PROJECTS\n### Finance Planner | Flask Web App\n"
              "- Rebuilt the service with PyTorch, cutting p99 latency from 180ms to 62ms.\n")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "resume.docx"
            export_docx.markdown_to_docx(md.splitlines(), output)
            doc = Document(output)
            bullet = next(p for p in doc.paragraphs if p.text.startswith("Rebuilt the service"))
            self.assertEqual(bullet.text, "Rebuilt the service with PyTorch, cutting p99 latency from 180ms to 62ms.")
            self.assertIn("1A7F37", {str(r.font.color.rgb) for r in bullet.runs})   # green: technical skill
            blue = [r for r in bullet.runs if str(r.font.color.rgb) == "1A5FB4"]   # blue: measurable result
            self.assertTrue(blue)
            self.assertTrue(all(r.underline for r in blue))

    def test_section_heading_carries_the_thin_rule(self):
        md = "# ALEX SAMPLE\n## SUMMARY\nA graduate.\n"
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "resume.docx"
            export_docx.markdown_to_docx(md.splitlines(), output)
            doc = Document(output)
            heading = next(p for p in doc.paragraphs if p.text == "SUMMARY")
            self.assertIsNotNone(heading._p.pPr.find(qn("w:pBdr")))

    @staticmethod
    def shaded(run):
        return run._element.rPr is not None and run._element.rPr.find(qn("w:shd")) is not None

    def test_key_strength_is_shaded_in_self_description(self):
        md = "# ALEX SAMPLE\n## SUMMARY\nHands-on experience with end-to-end delivery.\n"
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "resume.docx"
            export_docx.markdown_to_docx(md.splitlines(), output)
            line = next(p for p in Document(output).paragraphs if p.text.startswith("Hands-on"))
            self.assertTrue(any(self.shaded(r) for r in line.runs))

    def test_key_strength_is_off_inside_project_sections(self):
        md = ("# ALEX SAMPLE\n## KEY PROJECTS\n### Finance Planner | Web App\n"
              "- Built a privacy-preserving pipeline for the team.\n")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "resume.docx"
            export_docx.markdown_to_docx(md.splitlines(), output)
            bullet = next(p for p in Document(output).paragraphs if p.text.startswith("Built a privacy"))
            self.assertFalse(any(self.shaded(r) for r in bullet.runs))
