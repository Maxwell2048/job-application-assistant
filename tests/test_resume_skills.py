import copy
import unittest
from resume_skills import AI_ENGINEERING,apply_to_sections,apply_to_export,clean_skill_text


class ResumeSkillPreferencesTests(unittest.TestCase):
    def test_preferences_apply_to_skills_but_preserve_project_evidence(self):
        sections=[{"title":"Key Projects","lines":[{"kind":"bullet","text":"Trained YOLO11n on geological maps.","basis":[]}]},
                  {"title":"Technical Skills","lines":[{"kind":"text","text":"AI / ML: PyTorch · YOLOv8/YOLO11 · scikit-learn","basis":[]}]}]
        source={"text":AI_ENGINEERING,"kind":"resume","source":"base.docx","section":"Resume","line_start":1,"line_end":1}
        result=apply_to_sections(sections,source)
        self.assertIn("YOLO11n",result[0]["lines"][0]["text"])
        self.assertNotIn("YOLO",result[1]["lines"][0]["text"])
        self.assertEqual(next(line for line in result[1]["lines"] if line["text"] == AI_ENGINEERING)["basis"][0]["quote"],AI_ENGINEERING)
        self.assertEqual(len(apply_to_sections(result,source)[1]["lines"]),2)

    def test_new_declarations_require_a_real_source_in_assembly(self):
        sections=[{"title":"Skills","lines":[{"text":"Python","basis":[]}]}]
        apply_to_sections(sections,{"text":"Python"})
        self.assertEqual(len(sections[0]["lines"]),1)

    def test_older_doc_reexport_honours_preferences_without_mutating_source(self):
        parsed={"sections":[{"title":"Skills","blocks":[{"heading":"","lines":[{"kind":"text","text":"AI/ML: YOLO11n, PyTorch"}]}]},
                            {"title":"Projects","blocks":[{"heading":"GeoMind","lines":[{"kind":"bullet","text":"Trained YOLO11n."}]}]}]}
        original=copy.deepcopy(parsed)
        result=apply_to_export(parsed)
        self.assertEqual(parsed,original)
        self.assertIn("AI/ML: PyTorch", [line["text"] for block in result["sections"][0]["blocks"] for line in block["lines"]])
        self.assertEqual(result["sections"][1],original["sections"][1])
        self.assertEqual(apply_to_export(result),result)

    def test_cleanup_keeps_other_tools(self):
        self.assertEqual(clean_skill_text("AI/ML: PyTorch, YOLOv8/YOLO11, Ultralytics"),"AI/ML: PyTorch, Ultralytics")

    def test_confirmed_tooling_and_finetuning_keep_sources_and_come_first(self):
        from resume_skills import MODEL_ADAPTATION,LLM_TOOLING
        declared = [AI_ENGINEERING,MODEL_ADAPTATION,LLM_TOOLING]
        source={"text":"\n".join(declared),"kind":"resume","source":"base.docx","section":"Resume","line_start":1,"line_end":3}
        sections=[{"title":"Skills","lines":[{"kind":"text","text":"Python and SQL","basis":[]}]}]
        result=apply_to_sections(sections,source)[0]["lines"]
        self.assertEqual([line["text"] for line in result[:3]],[AI_ENGINEERING,MODEL_ADAPTATION,LLM_TOOLING])
        self.assertTrue(all(line["basis"][0]["quote"] in source["text"] for line in result[:3]))
        self.assertEqual(len(apply_to_sections(sections,source)[0]["lines"]),4)
