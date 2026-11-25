"""Regression tests for recurring defects found in the November 2026 generation review."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_resume
from job_summary import generate_summary, normalize_summary, validate_summary
from resume_skills import AI_ENGINEERING, LLM_TOOLING, MODEL_ADAPTATION, apply_to_sections
from tailor_cv import internal_wording, validate_basis


def match(rid="R1", verdict="related", **kwargs):
    return {"requirement_id": rid, "text": "React", "importance": "required",
            "category": "technical_skill", "processing_status": "ok", "verdict": verdict,
            "evidence": [{"quote": "A project", "project": "Synthetic"}],
            "missing_aspects": [], "needs_review": False, **kwargs}


def advice(rid="R1", **kwargs):
    return {"requirement_id": rid, "analysis": "分析", "next_step": "下一步",
            "knowledge_points": ["要点"], "exercise": "练习", "acceptance_check": "标准",
            "urgency": "before_apply", **kwargs}


class SummaryNormalizationTests(unittest.TestCase):
    def test_rule_violations_are_corrected_not_fatal(self):
        matches = [match("R1", "direct"), match("R2", processing_status="error"),
                   match("R3", category="work_authorization"), match("R4")]
        data = {"overview": "总结", "advice": [advice("R1"), advice("R2"), advice("R3"),
                                              advice("R4", exercise=""), advice("R9"), advice("R1")]}
        notes = normalize_summary(data, matches, [])
        self.assertEqual(validate_summary(data, matches, []), [])
        by_id = {a["requirement_id"]: a for a in data["advice"]}
        self.assertEqual(sorted(by_id), ["R1", "R2", "R3", "R4"])
        self.assertEqual(by_id["R1"]["urgency"], "before_interview")
        self.assertEqual((by_id["R2"]["urgency"], by_id["R2"]["knowledge_points"]), ("confirm_first", []))
        self.assertEqual((by_id["R3"]["exercise"], by_id["R3"]["urgency"]), ("", "confirm_first"))
        self.assertEqual(by_id["R4"]["knowledge_points"], [])
        self.assertTrue(notes)

    def test_missing_requirement_is_still_rejected(self):
        data = {"overview": "总结", "advice": [advice("R1", urgency="later")]}
        normalize_summary(data, [match("R1"), match("R2")], [])
        self.assertTrue(validate_summary(data, [match("R1"), match("R2")], []))

    def test_generate_summary_records_normalization(self):
        class Local:
            model = "synthetic"
            def generate_json(self, task, prompt, payload, schema, validate):
                data = {"overview": "总结", "advice": [advice("R1")]}
                assert not validate(data)
                return data
        result = generate_summary(Local(), [match("R1", "direct")])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["advice"][0]["urgency"], "before_interview")
        self.assertIn("normalized", result)


class InternalWordingTests(unittest.TestCase):
    LEAKS = [
        "Contributed 30 commits; also assisted the visualisation module (self-reported, no personal commits).",
        "Curated an 11-project knowledge base; corrected prior resume inaccuracies (phantom WebSocket/Selenium).",
        "Curated projects and corrected resume draft discrepancies against GitHub history.",
        "Two-person project; no per-module ownership recorded, so work cannot be attributed to me.",
        "Tagged unverified facts to preserve provenance transparency.",
    ]
    CLEAN = [
        "Built a local RAG application with evidence-based validation and source-linked citations.",
        "Co-developed the ETL, star schema and Power BI dashboard with a teammate.",
        "Supported the team's visualisation module and unittest suite.",
        "Verified the full pipeline end-to-end via the web interface.",
        "Job Evidence RAG  |  Local LLM + Retrieval Pipeline",
    ]

    def test_leaks_detected_and_clean_text_passes(self):
        for text in self.LEAKS:
            self.assertTrue(internal_wording(text), text)
        for text in self.CLEAN:
            self.assertIsNone(internal_wording(text), text)

    def test_elided_quote_needs_ordered_verbatim_fragments(self):
        from tailor_cv import quote_in
        text = "主链路为：JD 原文 → 本地模型提取要求（带原文出处）→ E5 多语言向量检索个人经历章节 → 判断"
        self.assertTrue(quote_in("主链路为：JD 原文 → 本地模型提取要求...→ E5 多语言向量检索个人经历章节", text))
        self.assertTrue(quote_in("主链路为：JD 原文 → 本地模型提取要求…E5 多语言向量检索", text))
        self.assertFalse(quote_in("E5 多语言向量检索个人经历章节...主链路为：JD 原文", text))  # order
        self.assertFalse(quote_in("主链路为：JD 原文...虚构的内容片段", text))
        self.assertFalse(quote_in("主链路...判断", text))  # no fragment long enough to identify the source
        self.assertTrue(quote_in("主链路为：JD 原文 → 本地模型提取要求...E5...判断", text))  # short connectors in order

    def test_admin_and_end_user_support_claims_rejected_in_resume_text_only(self):
        by_id = {"M1": {"text": "我写了登录页面", "scope": "personal", "kind": "experience"}}
        item = {"text": "Built login pages, enabling user access management.",
                "basis": [{"material_id": "M1", "quote": "我写了登录页面"}]}
        self.assertTrue(any("authentication routes" in e for e in
                            validate_basis([item], by_id, ["M1"], english_only=True, resume_text=True)))
        self.assertEqual(validate_basis([item], by_id, ["M1"], english_only=True), [])  # cover letter
        item["text"] = "Built login/registration pages and authentication routes with Flask-Login."
        self.assertEqual(validate_basis([item], by_id, ["M1"], english_only=True, resume_text=True), [])

    def test_names_only_in_the_jd_are_rejected_in_resume_text(self):
        from tailor_cv import jd_only_names
        by_id = {"M1": {"text": "熟练使用 Windows 桌面；PostgreSQL 与 Git；Australia 105 airports",
                        "scope": "personal", "kind": "profile"}}
        jd = "Manage endpoints across Windows, macOS, iOS and Android. US-based team. Git-based workflow."
        self.assertEqual(jd_only_names("Comfortable across Windows, macOS, iOS and Android.", jd, by_id),
                         ["macOS", "iOS", "Android"])
        self.assertEqual(jd_only_names("Used Git-based workflows with PostgreSQL across US and Australian data.",
                                       jd + " Australian", by_id), [])
        item = {"text": "Supported Windows and Android devices.", "basis": [{"material_id": "M1", "quote": "熟练使用 Windows 桌面"}]}
        self.assertTrue(validate_basis([item], by_id, ["M1"], english_only=True, resume_text=True, jd_text=jd))
        self.assertEqual(validate_basis([item], by_id, ["M1"], english_only=True, jd_text=jd), [])  # letters may name gaps

    def test_verbatim_quote_under_wrong_id_is_reattributed(self):
        by_id = {"M1": {"text": "我写了解析器", "scope": "personal", "kind": "experience"},
                 "RESUME": {"text": "System: (team) The team integrated the detector.", "scope": "personal",
                            "kind": "resume"}}
        item = {"text": "Integrated the detector with the team.",
                "basis": [{"material_id": "M1", "quote": "System: (team) The team integrated"}]}
        self.assertEqual(validate_basis([item], by_id, ["M1", "RESUME"], english_only=True), [])
        self.assertEqual(item["basis"][0]["material_id"], "RESUME")
        item["basis"][0] = {"material_id": "M1", "quote": "invented text"}
        self.assertTrue(validate_basis([item], by_id, ["M1", "RESUME"], english_only=True))

    def test_rather_without_than_is_repaired_in_place(self):
        by_id = {"M1": {"text": "提升度接近 1", "scope": "personal", "kind": "experience"}}
        item = {"text": "Lift near 1 indicated weak associations rather predictive accuracy.",
                "basis": [{"material_id": "M1", "quote": "提升度接近 1"}]}
        self.assertEqual(validate_basis([item], by_id, ["M1"], english_only=True), [])
        self.assertEqual(item["text"], "Lift near 1 indicated weak associations rather than predictive accuracy.")
        self.assertTrue(internal_wording("Two-person project with no recorded module split."))

    def test_bullets_written_for_top_projects_and_every_job(self):
        from tailor_cv import tailored_projects
        order = [{"project": f"P{i}", "evidence_score": 10 - i} for i in range(9)] + [{"project": "Shop", "evidence_score": 0}]
        by_project = {f"P{i}": [{"text": "x"}] for i in range(9)}
        by_project["Shop"] = [{"text": "- 经历类型：工作经历"}]
        picked = [p["project"] for p in tailored_projects(order, by_project, limit=6)]
        self.assertEqual(picked, ["P0", "P1", "P2", "P3", "P4", "P5", "Shop"])

    def test_resume_text_with_internal_wording_is_rejected_with_guidance(self):
        by_id = {"M1": {"text": "我协助了可视化模块", "scope": "personal", "kind": "experience"}}
        item = {"text": "Assisted the visualisation module (self-reported).",
                "basis": [{"material_id": "M1", "quote": "我协助了可视化模块"}]}
        errors = validate_basis([item], by_id, ["M1"], english_only=True)
        self.assertTrue(any("supported the team" in e for e in errors))
        item["text"] = "Supported the team's visualisation module."
        self.assertEqual(validate_basis([item], by_id, ["M1"], english_only=True), [])


RESUME = ("ALEX SAMPLE\nAI Engineer  ·  Machine Learning  ·  NLP\n📍  Perth, WA\n"
          "Master of IT  ·  AI Track\nCoursework: Data Warehousing · Business Intelligence\n"
          "◆ Languages  Python · SQL     ◆ Data & DB  PostgreSQL · Power BI\n"
          "◆ Web & Tools  Flask · Docker · Linux\n")
PROFILE = {"material_id": "M010", "kind": "profile", "source": "profile.md", "section": "办公软件",
           "line_start": 1, "line_end": 3,
           "text": "精通 Microsoft 365 办公套件\n熟练使用 Windows 桌面\n实践包括 requirements analysis 与 user stories"}
RESUME_ENTRY = {"material_id": "RESUME", "kind": "resume", "source": "resume", "section": "全文",
                "line_start": 1, "line_end": 7, "text": RESUME}


def header(*extra):
    lines = [{"kind": "text", "text": "ALEX SAMPLE", "basis": [{"material_id": "RESUME", "quote": "ALEX SAMPLE"}]}]
    lines += [{"kind": "text", "text": t, "basis": [{"material_id": "RESUME", "quote": t}]} for t in extra]
    return [{"title": "Header", "lines": lines}]


class HeadlineTests(unittest.TestCase):
    def family(self, title):
        return build_resume.job_family(title)

    def test_families(self):
        cases = {"IT Support Analyst": "it", "Technical Support Analyst": "it", "Test Analyst": "software",
                 "Data Analyst": "data", "Power BI Specialist": "data", "Oracle Database Administrator": "data",
                 "Cyber Security Analyst (Cloud & IAM)": "security", "AI Engineer": "ai",
                 "Software Engineer": "software", "AlayaCare Business Systems Owner": "business",
                 "Monthly Business Review Analyst": "data", "Modern Workplace Engineer": "it",
                 "Capgemini Graduate Program": None}
        for title, family in cases.items():
            self.assertEqual(self.family(title), family, title)

    def test_headline_targets_job_and_cites_sources(self):
        notes = []
        sections = build_resume.target_headline(
            header("AI Engineer  ·  Machine Learning  ·  NLP", "📍  Perth, WA"),
            "Title: IT Support Officer\n", [PROFILE, RESUME_ENTRY], RESUME_ENTRY, notes)
        line = sections[0]["lines"][1]
        self.assertEqual(line["text"], build_resume.headline_for("it"))
        quotes = {b["quote"] for b in line["basis"]}
        self.assertIn("精通 Microsoft 365 办公套件", quotes)
        self.assertTrue(all(q in RESUME or q in PROFILE["text"] for q in quotes))
        self.assertTrue(notes)

    def test_ai_job_keeps_base_and_dropped_headline_is_restored(self):
        sections = build_resume.target_headline(header("📍  Perth, WA"), "Title: Machine Learning Engineer",
                                                [PROFILE, RESUME_ENTRY], RESUME_ENTRY)
        self.assertEqual(sections[0]["lines"][1]["text"], "AI Engineer  ·  Machine Learning  ·  NLP")

    def test_unsupported_term_falls_back_to_base(self):
        sections = build_resume.target_headline(header("AI Engineer  ·  Machine Learning  ·  NLP"),
                                                "Title: IT Support Officer", [RESUME_ENTRY], RESUME_ENTRY)
        self.assertEqual(sections[0]["lines"][1]["text"], "AI Engineer  ·  Machine Learning  ·  NLP")

    def test_heading_separators_and_case_normalized(self):
        self.assertEqual(build_resume._clean_heading("Graph DB | Neo4j · Cypher  |  individual project"),
                         "Graph DB  |  Neo4j · Cypher  |  Individual project")
        self.assertEqual(build_resume._clean_heading("RAG | Tool | tool"), "RAG  |  Tool")


class QualityReportTests(unittest.TestCase):
    def test_report_flags_the_defects_reviewed_by_hand(self):
        import json
        import tempfile
        import quality_report
        cite = [{"material_id": "RESUME", "quote": "x"}]
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "jd.txt").write_text("Title: IT Support Officer\n\nSupport Microsoft 365 users.\n", encoding="utf-8")
            (run / "cv_suggestions.json").write_text(json.dumps({
                "project_order": [{"project": "RAG"}, {"project": "Web"}], "projects": [{"project": "Web"}],
                "errors": [{"step": "bullets:RAG", "message": "quote"}], "summary": {"text": "s"},
                "cover_letter": [{"text": "p"}]}), encoding="utf-8")
            (run / "job_summary.json").write_text(json.dumps({"status": "complete", "normalized": ["a"]}),
                                                  encoding="utf-8")
            (run / "resume_tailored.json").write_text(json.dumps({"sections": [
                {"title": "Header", "lines": [{"kind": "text", "text": "ALEX", "basis": cite},
                                              {"kind": "text", "text": "AI Engineer  ·  NLP", "basis": cite}]},
                {"title": "Summary", "lines": [{"kind": "text", "text": "Graduate (self-reported) with service desk focus.",
                                                "basis": cite}]},
                {"title": "Skills", "lines": [{"kind": "text", "text": AI_ENGINEERING, "basis": cite},
                                              {"kind": "text", "text": "Languages: Python · SQL", "basis": cite}]}]}),
                encoding="utf-8")
            report = quality_report.assess(run, materials=[PROFILE, RESUME_ENTRY], resume_text=RESUME)
        checks = {c["key"]: c for c in report["checks"]}
        self.assertEqual(report["level"], "bad")
        self.assertEqual(checks["projects"]["level"], "bad")          # a top project was lost
        self.assertIn("self-reported", checks["wording"]["detail"])
        self.assertIn("service desk", checks["wording"]["detail"])
        self.assertEqual(checks["headline"]["level"], "warn")
        self.assertIn("AI Engineering", checks["skills"]["detail"])
        self.assertEqual(checks["docx"]["level"], "bad")
        self.assertEqual(checks["readiness"]["level"], "ok")
        self.assertEqual(quality_report.headline(report), "3 项问题；2 项提醒")


class SchemaExampleTests(unittest.TestCase):
    def test_nullable_types_get_an_example(self):
        """job_identity's schema uses ["string", "null"]; the example builder crashed on it."""
        from local_llm import schema_example
        schema = {"type": "object", "required": ["company", "job_title"],
                  "properties": {"company": {"type": ["string", "null"]}, "job_title": {"type": "string"}}}
        self.assertEqual(schema_example(schema), {"company": "<replace with input-grounded text>",
                                                  "job_title": "<replace with input-grounded text>"})
        self.assertIsNone(schema_example({"type": ["null"]}))


class SupportRoleTests(unittest.TestCase):
    WORK = {"material_id": "M050", "kind": "experience", "project": "Shop", "section": "基本信息",
            "text": "- 经历类型：工作经历\n- 职位：楼面员工"}
    SUGGESTIONS = {"projects": [{"project": "RAG", "bullets": [{"text": "a"}, {"text": "b"}]},
                                {"project": "Shop", "bullets": [{"text": "c"}, {"text": "d"}]}]}

    def refs(self):
        cite = [{"material_id": "M050", "quote": "x"}]
        return {k: {"text": t, "basis": cite} for k, t in
                {"H01": "RAG  |  Project", "B01": "a", "B02": "b", "H02": "Shop  |  Store Assistant", "B03": "c", "B04": "d"}.items()}

    def line(self, ref, kind):
        return {"kind": kind, "text": self.refs()[ref]["text"], "basis": [], "_from_ref": ref}

    def test_job_filed_under_projects_moves_to_work_experience(self):
        data = {"sections": [{"title": "Header", "lines": [{"kind": "text", "text": "ALEX", "basis": []}]},
                             {"title": "Projects", "lines": [self.line("H01", "heading"), self.line("B01", "bullet"),
                                                             self.line("H02", "heading"), self.line("B03", "bullet")]},
                             {"title": "Education", "lines": [{"kind": "text", "text": "UWA", "basis": []}]}]}
        build_resume.place_work_experience(data, self.SUGGESTIONS, [self.WORK], self.refs())
        self.assertEqual([s["title"] for s in data["sections"]], ["Header", "Projects", "Work Experience", "Education"])
        self.assertEqual([l["_from_ref"] for l in data["sections"][2]["lines"]], ["H02", "B03"])
        self.assertEqual([l["_from_ref"] for l in data["sections"][1]["lines"]], ["H01", "B01"])

    def test_job_left_out_by_assembly_is_added(self):
        data = {"sections": [{"title": "Projects", "lines": [self.line("H01", "heading")]},
                             {"title": "Education", "lines": [{"kind": "text", "text": "UWA", "basis": []}]}]}
        notes = []
        build_resume.place_work_experience(data, self.SUGGESTIONS, [self.WORK], self.refs(), notes)
        work = data["sections"][1]
        self.assertEqual((work["title"], [l["_from_ref"] for l in work["lines"]]), ("Work Experience", ["H02", "B03", "B04"]))
        self.assertTrue(notes)

    def test_no_work_material_changes_nothing(self):
        data = {"sections": [{"title": "Projects", "lines": [self.line("H01", "heading")]}]}
        build_resume.place_work_experience(data, self.SUGGESTIONS, [], self.refs())
        self.assertEqual([s["title"] for s in data["sections"]], ["Projects"])

    def test_home_networking_row_for_it_roles_only(self):
        profile = {"material_id": "M010", "kind": "profile", "source": "profile.md", "section": "家庭网络",
                   "line_start": 1, "line_end": 2,
                   "text": "- 我给家里的 Wi-Fi 路由器刷入了 Asuswrt-Merlin（梅林）第三方固件，之后自行升级固件。"}
        self.assertEqual(build_resume.networking_line("Title: IT Support Officer\n", [profile])[0]["text"],
                         build_resume.NETWORKING)
        self.assertEqual(build_resume.networking_line("Title: Data Analyst\nSQL and Power BI.", [profile]), [])
        self.assertEqual(build_resume.networking_line("Title: IT Support Officer\n", [PROFILE]), [])

    def test_llm_frameworks_row_is_cited_and_grouped_with_ai_tooling(self):
        from resume_skills import LLM_FRAMEWORKS, LLM_TOOLING, order_by_jd
        profile = {"material_id": "M011", "kind": "profile", "source": "profile.md", "section": "大模型应用",
                   "line_start": 1, "line_end": 2,
                   "text": "本人技能补充（2026-11-25 确认）：实际使用 LangChain 和 LangGraph。"}
        row = build_resume.frameworks_line("Title: AI Engineer\nBuild LLM agents.", [profile])
        self.assertEqual(row[0]["text"], LLM_FRAMEWORKS)
        self.assertEqual(row[0]["basis"][0]["material_id"], "M011")
        self.assertTrue(build_resume.frameworks_line("Title: Analyst\nExperience with LangChain.", [profile]))
        self.assertEqual(build_resume.frameworks_line("Title: IT Support Officer\nPrinters.", [profile]), [])
        self.assertEqual(build_resume.frameworks_line("Title: AI Engineer\n", [PROFILE]), [])   # no confirmation
        lines = [{"text": LLM_TOOLING}, {"text": "Languages: Python · SQL"}, row[0]]
        ordered = [l["text"] for l in order_by_jd(lines, "Python and SQL")]
        self.assertEqual(ordered, ["Languages: Python · SQL", LLM_TOOLING, LLM_FRAMEWORKS])

    def test_support_focus_reaches_bullet_prompts_for_it_jobs(self):
        from tailor_cv import CVTailor, FAMILY_FOCUS

        class Capture:
            prompt_char_budget = 10 ** 9
            note = None
            def estimate_context(self, *a):
                return 0
            def generate_json(self, task, system, payload, schema, timeout, validate):
                Capture.note = payload["task_note"]
                return {"title": "P  |  Project", "bullets": []}
        own = {"material_id": "M1", "kind": "experience", "scope": "personal", "project": "P",
               "section": "我的工作", "text": "我写了解析器"}
        resume = {"material_id": "RESUME", "kind": "resume", "text": "X"}
        tailor = CVTailor(Capture(), [own, resume], {"P": [own]}, resume)
        tailor.family = "it"
        tailor.project_bullets("P", [])
        self.assertIn(FAMILY_FOCUS["it"], Capture.note)
        tailor.family = "data"
        tailor.project_bullets("P", [])
        self.assertNotIn(FAMILY_FOCUS["it"], Capture.note)


class AnswerLengthTests(unittest.TestCase):
    def test_overlong_answer_loses_trailing_paragraphs_never_the_first(self):
        from application_answer import trim_to_limit, word_count
        paragraphs = [{"text": "one two three four"}, {"text": "five six seven"}, {"text": "eight nine"}]
        self.assertEqual(word_count(trim_to_limit(paragraphs, 7)), 7)
        self.assertEqual(len(paragraphs), 2)
        single = [{"text": "a b c d e f"}]
        self.assertEqual(trim_to_limit(single, 2), single)   # one paragraph stays; validation reports it


class MaterialViewTests(unittest.TestCase):
    def test_provenance_lines_hidden_and_boundaries_not_citable(self):
        from tailor_cv import CVTailor, visible_materials, quote_in
        own = {"material_id": "M1", "kind": "experience", "scope": "personal", "project": "P",
               "section": "我的贡献 本人自述补充",
               "text": "本人自述：我还协助了可视化模块的开发与调试。\n证据：本人自述（2026-09-12 对话说明），无文件佐证。"}
        edge = {"material_id": "M2", "kind": "experience", "scope": "team", "project": "P", "section": "证据边界",
                "text": "当前成果为设计文档，不是已经运行的产品功能。\n证据：报告第 3 页。"}
        shown = visible_materials([own])
        self.assertNotIn("证据", shown[0]["text"])
        self.assertTrue(quote_in("我还协助了可视化模块的开发与调试", own["text"]))  # quotes still verify

        class Capture:
            prompt_char_budget = 10 ** 9
            payload = None
            def estimate_context(self, *a):
                return 0
            def generate_json(self, task, system, payload, schema, timeout, validate):
                Capture.payload = payload
                data = {"title": "P  |  Project", "bullets": [{"text": "Supported the team's visualisation module.",
                        "basis": [{"material_id": "M2", "quote": "当前成果为设计文档"}], "requirement_ids": []}]}
                assert validate(data), "a boundary section must not be citable"
                data["bullets"][0]["basis"] = [{"material_id": "M1", "quote": "我还协助了可视化模块的开发与调试"}]
                assert not validate(data)
                return data
        resume = {"material_id": "RESUME", "kind": "resume", "text": "X"}
        tailor = CVTailor(Capture(), [own, edge, resume], {"P": [own, edge]}, resume)
        tailor.project_bullets("P", [])
        self.assertEqual([m["material_id"] for m in Capture.payload["materials"]], ["M1"])
        self.assertEqual(Capture.payload["boundaries"], ["当前成果为设计文档，不是已经运行的产品功能。"])


class RetryRepairTests(unittest.TestCase):
    def test_second_attempt_repairs_grammar_and_drops_only_leaking_bullets(self):
        from tailor_cv import repair_after_retry
        bullets = [{"text": "Weak associations rather predictive accuracy."},
                   {"text": "Assisted testing (self-reported)."}]
        self.assertEqual(repair_after_retry(bullets), 1)
        self.assertEqual([b["text"] for b in bullets], ["Weak associations rather than predictive accuracy."])
        only = [{"text": "Assisted testing (self-reported)."}]
        self.assertEqual(repair_after_retry(only), 0)
        self.assertEqual(len(only), 1)  # never empties a project; validation still reports it

    def test_quote_with_silent_omission_is_explained_then_trimmed(self):
        from tailor_cv import divergence_hint, trim_quotes_to_verbatim, quote_in
        text = "主链路为：JD 原文 → 本地模型提取要求 → E5 多语言向量检索 → 本地模型逐条判断证据 → 程序校验引用 → Markdown 与 JSON 报告。"
        quote = "主链路为：JD 原文 → 本地模型提取要求 → E5 多语言向量检索 → 本地模型逐条判断证据 → Markdown 与 JSON 报告。"
        self.assertIn("程序校验引用", divergence_hint(quote, text))
        bullets = [{"text": "Built the pipeline.", "basis": [{"material_id": "M1", "quote": quote}]}]
        trim_quotes_to_verbatim(bullets, {"M1": {"text": text}}, ["M1"])
        trimmed = bullets[0]["basis"][0]["quote"]
        self.assertTrue(quote_in(trimmed, text))
        self.assertTrue(trimmed.startswith("主链路为：JD 原文") and "Markdown" not in trimmed)
        wrong = [{"text": "x", "basis": [{"material_id": "M1", "quote": "完全不同的一句话，并不在原文里出现过的内容"}]}]
        trim_quotes_to_verbatim(wrong, {"M1": {"text": text}}, ["M1"])
        self.assertFalse(quote_in(wrong[0]["basis"][0]["quote"], text))

    def test_long_title_is_shortened_by_dropping_trailing_segments(self):
        from tailor_cv import shorten_title
        title = "Job Evidence RAG  |  Local LLM + Retrieval Pipeline  |  Individual Project (AI-Assisted Development)"
        self.assertEqual(shorten_title(title), "Job Evidence RAG  |  Local LLM + Retrieval Pipeline")
        self.assertEqual(shorten_title("Short  |  Title"), "Short  |  Title")

    def test_project_survives_a_persistent_leak(self):
        from tailor_cv import CVTailor

        class Stubborn:
            prompt_char_budget = 10 ** 9
            def estimate_context(self, *a):
                return 0
            def generate_json(self, task, system, payload, schema, timeout, validate):
                for _ in range(3):
                    data = {"title": "Sample  |  Project", "bullets": [
                        {"text": "Built the parser.", "basis": [{"material_id": "M1", "quote": "我写了解析器"}],
                         "requirement_ids": []},
                        {"text": "Assisted QA (self-reported).", "basis": [{"material_id": "M1", "quote": "我写了解析器"}],
                         "requirement_ids": []}]}
                    if not validate(data):
                        return data
                raise AssertionError("project lost")
        materials = [{"material_id": "M1", "kind": "experience", "scope": "personal", "project": "P",
                      "section": "我的贡献", "text": "我写了解析器"}]
        resume = {"material_id": "RESUME", "kind": "resume", "text": "X"}
        tailor = CVTailor(Stubborn(), materials + [resume], {"P": materials}, resume)
        data = tailor.project_bullets("P", [])
        self.assertEqual([b["text"] for b in data["bullets"]], ["Built the parser."])
        self.assertTrue(tailor.warnings)


class BaseSkillsTests(unittest.TestCase):
    def test_omitted_base_skill_rows_are_restored(self):
        text = "NAME\n▐  TECHNICAL SKILLS\n◆ Data & DB  PostgreSQL · Power BI\n◆ Languages  Python\n▐  LANGUAGES\nEnglish: IELTS\n"
        entry = {**RESUME_ENTRY, "text": text}
        sections = [{"title": "Skills", "lines": [{"kind": "text", "text": "Languages: Python",
                                                  "basis": [{"material_id": "RESUME", "quote": "◆ Languages  Python"}]}]}]
        notes = []
        build_resume.keep_base_skills(sections, entry, notes)
        self.assertEqual([b["quote"] for l in sections[0]["lines"] for b in l["basis"]],
                         ["◆ Languages  Python", "◆ Data & DB  PostgreSQL · Power BI"])
        self.assertTrue(notes)


class FillerAndExportTests(unittest.TestCase):
    def test_self_assessment_lines_are_removed_wherever_placed(self):
        text = "NAME\n▐  LANGUAGES\nEnglish: IELTS 6.5\n▐  SELF-ASSESSMENT\nProfile: Strong self-learner.\nTeamwork: Core member.\n"
        entry = {**RESUME_ENTRY, "text": text}
        cite = lambda q: [{"material_id": "RESUME", "quote": q}]
        sections = [{"title": "Additional", "lines": [
            {"kind": "text", "text": "Profile: Strong self-learner.", "basis": cite("Profile: Strong self-learner.")},
            {"kind": "text", "text": "Teamwork: Core member.", "basis": cite("Teamwork: Core member.")}]},
            {"title": "Languages", "lines": [{"kind": "text", "text": "English: IELTS 6.5", "basis": cite("English: IELTS 6.5")}]}]
        notes = []
        result = build_resume.drop_self_assessment(sections, entry, notes)
        self.assertEqual([s["title"] for s in result], ["Languages"])
        self.assertTrue(notes)

    def test_additional_section_drops_grouping_labels(self):
        section = {"title": "Additional", "lines": [{"kind": "text", "text": t, "basis": []} for t in
                                                    ["Creative & Media Skills", "Video production: editing."]]}
        result, _ = build_resume.trim_projects([section])
        self.assertEqual([l["text"] for l in result[0]["lines"]], ["Video production: editing."])

    def test_export_keeps_job_ordered_skills(self):
        from resume_skills import apply_to_export
        rows = ["Data & DB: PostgreSQL · Power BI", AI_ENGINEERING, MODEL_ADAPTATION, LLM_TOOLING]
        parsed = {"sections": [{"title": "Skills", "blocks": [{"heading": "", "lines": [
            {"kind": "text", "text": t} for t in rows]}]}]}
        out = apply_to_export(parsed)
        self.assertEqual([l["text"] for b in out["sections"][0]["blocks"] for l in b["lines"]], rows)


class LanguageSectionTests(unittest.TestCase):
    def test_sub_labels_dropped_and_other_lines_kept_apart(self):
        texts = ["Language Proficiency", "English: IELTS Overall 6.5", "Mandarin Chinese: Native speaker",
                 "Creative & Media Skills", "Video production: editing and post-production"]
        section = {"title": "Languages", "lines": [{"kind": "text", "text": t, "basis": []} for t in texts]}
        result, _ = build_resume.trim_projects([section])
        self.assertEqual([l["text"] for l in result[0]["lines"]],
                         ["English: IELTS Overall 6.5 | Mandarin Chinese: Native speaker",
                          "Video production: editing and post-production"])


class SkillOrderTests(unittest.TestCase):
    def sections(self):
        raw = ["◆ AI / ML  PyTorch · Scikit-learn     ◆ Data & DB  PostgreSQL · Power BI · DAX",
               "◆ Languages  Python · SQL", AI_ENGINEERING, MODEL_ADAPTATION, LLM_TOOLING]
        return [{"title": "Skills", "lines": [{"kind": "text", "text": t, "basis": [{"material_id": "RESUME", "quote": t}]}
                                             for t in raw]}]

    def test_categories_split_and_ordered_by_jd(self):
        source = {"text": "\n".join([AI_ENGINEERING, MODEL_ADAPTATION, LLM_TOOLING]), "kind": "resume",
                  "source": "resume", "section": "全文", "line_start": 1, "line_end": 3}
        lines = apply_to_sections(self.sections(), source, "Build Power BI reports with DAX and SQL.")[0]["lines"]
        texts = [l["text"] for l in lines]
        self.assertEqual(texts[0], "Data & DB: PostgreSQL · Power BI · DAX")
        self.assertEqual(texts[1], "Languages: Python · SQL")
        declared = [texts.index(t) for t in (AI_ENGINEERING, MODEL_ADAPTATION, LLM_TOOLING)]
        self.assertEqual(declared, list(range(declared[0], declared[0] + 3)))  # kept together
        self.assertTrue(all(l["basis"] for l in lines))

    def test_productivity_row_only_when_jd_asks_and_profile_supports(self):
        self.assertEqual(build_resume.productivity_line("Python and SQL", [PROFILE]), [])
        self.assertEqual(build_resume.productivity_line("Support Microsoft 365 users", [RESUME_ENTRY]), [])
        row = build_resume.productivity_line("Support Microsoft 365 and Windows 11 users", [PROFILE])
        self.assertEqual(row[0]["text"], build_resume.PRODUCTIVITY)
        self.assertTrue(all(b["quote"] in PROFILE["text"] for b in row[0]["basis"]))
        self.assertNotRegex(row[0]["text"], r"(?i)intune|entra|exchange|admin")

    def test_non_ai_role_puts_general_rows_before_research_rows_on_ties(self):
        source = {"text": "", "kind": "resume", "source": "resume", "section": "全文", "line_start": 1, "line_end": 1}
        lines = apply_to_sections(self.sections(), source, "Friendly team player.", research_last=True)[0]["lines"]
        labels = [l["text"].split(":")[0] for l in lines]
        self.assertEqual(labels[:3], ["Data & DB", "Languages", "AI / ML"])
        self.assertEqual(labels[-3:], ["AI Engineering", "Model Fine-tuning", "LLM Tooling"])
        self.assertEqual(build_resume.productivity_line("Title: IT Support Officer\nFriendly team.", [PROFILE])[0]["text"],
                         build_resume.PRODUCTIVITY)

    def test_items_repeated_across_rows_are_listed_once(self):
        from resume_skills import dedupe_items
        lines = [{"text": "Model Fine-tuning: LoRA · Transfer Learning"},
                 {"text": "Deep Learning: CNN · transfer learning · GRU"}, {"text": "Only: transfer learning"}]
        self.assertEqual([l["text"] for l in dedupe_items(lines)],
                         ["Model Fine-tuning: LoRA · Transfer Learning", "Deep Learning: CNN · GRU",
                          "Only: transfer learning"])

    def test_ai_role_keeps_declared_group_first_even_without_keywords(self):
        source = {"text": "", "kind": "resume", "source": "resume", "section": "全文", "line_start": 1, "line_end": 1}
        lines = apply_to_sections(self.sections(), source, "Python and SQL.", declared_first=True)[0]["lines"]
        self.assertEqual(lines[0]["text"], AI_ENGINEERING)

    def test_ai_job_puts_declared_tooling_group_first(self):
        source = {"text": "", "kind": "resume", "source": "resume", "section": "全文", "line_start": 1, "line_end": 1}
        lines = apply_to_sections(self.sections(), source,
                                  "LLM work with Ollama, LoRA, MCP and Hugging Face Transformers.")[0]["lines"]
        self.assertEqual(lines[0]["text"], AI_ENGINEERING)


if __name__ == "__main__":
    unittest.main()
