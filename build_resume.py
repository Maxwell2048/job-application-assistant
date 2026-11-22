"""Assemble a complete tailored resume (Markdown) from a match_job run.

    python build_resume.py --run outputs/<run> --resume resume/base_resume.docx --config config.local.json

Inputs: the run's latest cv_suggestions*.json (from tailor_cv.py), the base
resume, experiences/ materials and profile.md. One model call arranges the
document as sections of lines; every line carries a material id and a
verbatim quote, checked by the program exactly as in tailor_cv (substring,
numbers, JD wording named). The body is written clean; a source appendix
maps every line back to its evidence so a reviewer can delete it before
sending. Output: resume_tailored.md / .json next to the suggestions.
"""
import argparse
import datetime
import json
import copy
import re
import sys
import zipfile
from pathlib import Path

from local_llm import LLMError, build_llm, load_config
from job_family import HEADLINES, headline_for, job_family, job_title  # noqa: F401 (re-exported)
from resume_skills import apply_to_sections
from resume_review import review_projects
from tailor_cv import (WORK_TYPE, RESUME_ID, KIND_LABELS, UNVERIFIED_KINDS, CVTailor, _attach_sources, latest_output,
                       load_materials, load_run, read_resume, requirement_summary, stale_citations, validate_basis, non_english,
                       unique_output)

PROJECT_ROOT = Path(__file__).resolve().parent
BUILD_PROMPT_VERSION = "7"
MAX_LINE_CHARS = 500
# Page budget enforced by the program: models ignore 'keep it short' guidance.
MAX_KEY_PROJECTS, MAX_KEY_BULLETS = 4, 4
MAX_ADDITIONAL_PROJECTS, MAX_ADDITIONAL_BULLETS = 2, 2
MAX_SKILL_LINES = 6  # guidance; supported skill facts are not silently deleted

SYSTEM_PROMPT = """Arrange an English resume by selecting existing reference IDs. Do not write new resume text.
Return one complete JSON object matching the schema. Each line has exactly two fields: kind and ref.
kind is the string "heading", "bullet" or "text". ref is a string ID from reference_catalog.
Never output a schema definition as a value. Close every object and array. Do not include commentary or Markdown fences.
The program inserts the verified text and citations for each selected ID.
Use a Header section first; its first line must reference the candidate name. Keep contact details in Header.
Include SUMMARY unchanged in its own Summary section when supplied.
Use the H reference for each project heading and that project's own B references for its bullets.
Choose 3–4 relevant main projects with 2–4 bullets each; optionally add 1–2 supporting projects with 1–2 bullets each.
These are upper bounds, not quotas. Avoid duplicate projects and irrelevant detail. Aim for a clean one-to-two-page resume.
Preserve useful Education, Skills and Languages from R references. Course codes have already been removed.
Respect review actions for base-resume lines: omit remove entries and prioritize move_up entries.
Do not select old project bullets from the base resume when a tailored version exists.
Use short English section titles. ID text, facts, personal ownership and metric qualifiers must remain unchanged.
Example shape only: {"sections":[{"title":"Header","lines":[{"kind":"text","ref":"R001"}]}]}
Use only IDs actually present in reference_catalog; do not copy an example ID if it is absent.
"""

RESUME_SCHEMA = {
    "type": "object",
    "required": ["sections"],
    "additionalProperties": False,
    "properties": {"sections": {"type": "array", "minItems": 2, "items": {
        "type": "object", "required": ["title", "lines"], "additionalProperties": False,
        "properties": {
            "title": {"type": "string", "minLength": 1},
            "lines": {"type": "array", "minItems": 1, "items": {
                "type": "object", "required": ["kind"],
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string", "enum": ["heading", "bullet", "text"]},
                    "ref": {"type": "string", "minLength": 1},
                    "text": {"type": "string", "minLength": 1},
                    "basis": {"type": "array", "minItems": 1, "items": {
                        "type": "object", "required": ["material_id", "quote"],
                        "additionalProperties": False,
                        "properties": {"material_id": {"type": "string"},
                                       "quote": {"type": "string", "minLength": 1}}}},
                }}},
        }}}},
}


def latest_suggestions(run_dir):
    path = latest_output(run_dir, "cv_suggestions", ".json")
    if path is None:
        raise ValueError(f"{run_dir} 里没有 cv_suggestions*.json；先运行 tailor_cv.py")
    return path


def referenced_lines(suggestions):
    """id -> {text, basis} for every tailored bullet (B01, B02...) and SUMMARY,
    so the model can cite them by id and the program fills the rest in."""
    refs = {}
    counter = 0
    for p in suggestions.get("projects", []):
        for b in p["bullets"]:
            counter += 1
            refs[f"B{counter:02d}"] = {"text": b["text"], "basis": [
                {"material_id": x["material_id"], "quote": x["quote"]} for x in b["basis"]]}
    summary = suggestions.get("summary")
    if summary:
        refs["SUMMARY"] = {"text": summary["text"], "basis": [
            {"material_id": x["material_id"], "quote": x["quote"]} for x in summary["basis"]]}
    return refs


def assembly_references(suggestions, resume_entry):
    """Selectable facts and upstream-validated titles, with program-owned citations."""
    refs = referenced_lines(suggestions)
    for index, project in enumerate(suggestions.get("projects", []), 1):
        title = project.get("title")
        bullets = project.get("bullets") or []
        if title and not non_english(title) and bullets and bullets[0].get("basis"):
            refs[f"H{index:02d}"] = {"text": title, "basis": [
                {"material_id": b["material_id"], "quote": b["quote"]} for b in bullets[0]["basis"]]}
    for index, raw in enumerate(resume_entry["text"].splitlines(), 1):
        raw = raw.strip()
        if not raw or re.match(r"^[▐▏▌]\s*", raw):
            continue
        # Strip course identifiers deterministically; the citation stays verbatim.
        text = re.sub(r"\b[A-Z]{4}\d{4}\b\s*[:|–—-]?\s*", "", raw).strip()
        if text and not non_english(text):
            refs[f"R{index:03d}"] = {"text": text, "basis": [{"material_id": RESUME_ID, "quote": raw}]}
    return refs


def assembly_schema(refs):
    """The model selects IDs; source text/citations are never regenerated."""
    schema = copy.deepcopy(RESUME_SCHEMA)
    line = schema["properties"]["sections"]["items"]["properties"]["lines"]["items"]
    line["required"] = ["kind", "ref"]
    line["properties"] = {"kind": line["properties"]["kind"],
                          "ref": {"type": "string", "enum": list(refs)}}
    return schema


def build_payload(suggestions, materials, resume_entry, jd_text):
    profile = [{"material_id": m["material_id"], "section": m["section"], "text": m["text"]}
               for m in materials if m["kind"] == "profile"]
    refs = referenced_lines(suggestions)
    projects, counter = [], 0
    for p in suggestions.get("projects", []):
        bullets = []
        for b in p["bullets"]:
            counter += 1
            bullets.append({"id": f"B{counter:02d}", "text": b["text"]})
        # "title" is the English heading from tailor_cv; "project" is the material's own
        # (often Chinese) name and must never be copied into the resume.
        projects.append({"title": p.get("title") or None, "material_name": p["project"], "bullets": bullets})
    return {
        "task_note": "只选 reference_catalog 的 id。每行输出 kind+ref；程序回填已验证的文本和来源，不重写内容或引用。",
        "reference_catalog": [{"id": key, "text": entry["text"]}
                              for key, entry in assembly_references(suggestions, resume_entry).items()],
        "jd_text": jd_text,
        "base_resume": {"material_id": RESUME_ID, "text": resume_entry["text"]},
        "profile": profile,
        "summary": ({"id": "SUMMARY", "text": refs["SUMMARY"]["text"]} if "SUMMARY" in refs else None),
        "tailored_projects": projects,
        "review": [{"quote": r["quote"], "action": r["action"], "reason": r["reason"][:120]}
                   for r in suggestions.get("resume_review", [])],
        "gaps": [{"requirement_id": g["requirement_id"], "text": g["text"], "kind": g["kind"]}
                 for g in suggestions.get("gaps", [])],
    }


def compact_assembly_payload(payload):
    """One copy of each verified line; assembly selects IDs, never rewrites facts.

    Full profile, base resume and project text duplicate the catalog and create
    conflicting citation-writing instructions for smaller local models.
    """
    return {
        "reference_catalog": payload["reference_catalog"],
        "summary_ref": "SUMMARY" if payload.get("summary") else None,
        "projects": [{"heading_ref": f"H{index:02d}",
                      "bullet_refs": [bullet["id"] for bullet in project["bullets"]]}
                     for index, project in enumerate(payload["tailored_projects"], 1)],
        "review": [{"quote": entry["quote"], "action": entry["action"]}
                   for entry in payload["review"]],
        "jd_text": payload["jd_text"][:1500],
    }


def allowed_material_ids(suggestions, materials):
    ids = [RESUME_ID] + [m["material_id"] for m in materials if m["kind"] == "profile"]
    for p in suggestions.get("projects", []):
        for b in p["bullets"]:
            ids += [x["material_id"] for x in b["basis"]]
    if suggestions.get("summary"):
        ids += [x["material_id"] for x in suggestions["summary"]["basis"]]
    return list(dict.fromkeys(ids))


def required_lines(suggestions, resume_text):
    """Exact lines the assembled resume must contain: the base resume's first
    line (the candidate's name) and the generated summary, if any."""
    required = []
    first = next((line.strip() for line in resume_text.splitlines() if line.strip()), "")
    if first:
        required.append(("底稿第一行（姓名）", first, 1))
    summary = suggestions.get("summary")
    if summary and summary.get("text"):
        required.append(("已生成的 summary 原文", summary["text"].strip(), None))
    return required


def expand_refs(data, refs):
    """Replace {ref: id} lines with the referenced text and basis, in place.
    Returns error strings for unknown ids or lines with neither ref nor text."""
    errors = []
    sections = data.get("sections", []) if isinstance(data, dict) else []
    for s_index, section in enumerate(sections, 1):
        for l_index, line in enumerate(section.get("lines", []), 1):
            if not isinstance(line, dict):
                continue
            ref = line.pop("ref", None)
            if ref is not None:
                if ref not in refs:
                    errors.append(f"第 {s_index} 节第 {l_index} 行引用了不存在的 ref {ref!r}")
                    continue
                line["text"] = refs[ref]["text"]
                line["basis"] = [dict(b) for b in refs[ref]["basis"]]
                # Filled in by the program from suggestions that tailor_cv already
                # validated against the project's full material; the model cannot
                # change it, so re-checking it here could only cause doomed retries.
                line["_from_ref"] = ref
            elif not line.get("text") or not line.get("basis"):
                errors.append(f"第 {s_index} 节第 {l_index} 行既没有 ref 也没有完整的 text+basis")
    return errors


def validate_document(data, by_id, allowed, jd_text, required=(), refs=None):
    errors = expand_refs(data, refs or {})
    sections = data.get("sections", []) if isinstance(data, dict) else []
    for label, text, section_index in required:
        scope = sections[section_index - 1:section_index] if section_index else sections
        texts = [line.get("text", "").strip() for s in scope for line in s.get("lines", [])]
        if text not in texts and section_index == 1 and sections \
                and isinstance(sections[0].get("lines"), list):
            # The name is a fixed fact from the base resume: insert it rather than
            # spend one of three attempts asking the model to copy it.
            sections[0]["lines"].insert(0, {"kind": "text", "text": text, "_from_ref": "NAME",
                                            "basis": [{"material_id": RESUME_ID, "quote": text}]})
            continue
        if text not in texts:
            where = f"第 {section_index} 节" if section_index else "任一节"
            errors.append(f"{where}缺少{label}：必须有一行 text 与之完全一致：{text[:80]!r}")
    for s_index, section in enumerate(sections, 1):
        lines = [l for l in section.get("lines", []) if isinstance(l, dict) and l.get("text")]
        for l_index, line in enumerate(lines, 1):
            if line.get("_from_ref"):
                continue  # program-filled and already validated upstream
            errors += validate_basis([line], by_id, allowed,
                                     label=f"第 {s_index} 节第 {l_index} 行", jd_text=jd_text,
                                     no_course_codes=True, english_only=True)
        for l_index, line in enumerate(lines, 1):
            if line.get("_from_ref"):
                continue  # the model cannot shorten program-filled text; asking it to only breaks the ref
            if len(line.get("text", "")) > MAX_LINE_CHARS:
                errors.append(f"第 {s_index} 节第 {l_index} 行超过 {MAX_LINE_CHARS} 字符，请拆分或精简")
    return errors


def ensure_summary(llm, suggestions, matches, jd_text, materials, by_project, resume_entry,
                   timeout_seconds=None):
    """A resume needs its SUMMARY. When tailor_cv could not produce one, try
    once more here, on its own. Returns a note for the appendix, or None."""
    if suggestions.get("summary"):
        return None
    tailor = CVTailor(llm, materials, by_project, resume_entry, timeout_seconds)
    try:
        data = tailor.summary(requirement_summary(matches), suggestions.get("projects", []),
                              suggestions.get("gaps", []), jd_text)
    except LLMError as exc:
        return f"SUMMARY 未生成：定制建议里没有摘要，组装时补生成也失败（{str(exc)[:120]}）；请手动补写"
    suggestions["summary"] = _attach_sources(data["summary"], tailor.by_id)
    return "SUMMARY 是组装时补生成的（定制建议那一步没有产出摘要）"


def assemble_verified_references(suggestions, resume_entry, refs):
    """Deterministic layout after malformed model output, using existing facts only.

    Project order was chosen upstream from the job match. Never repair a broken
    JSON fragment or invent missing content. Fail closed if core sections cannot
    be identified in this base resume.
    """
    groups = {"Header": [], "Education": [], "Skills": [], "Languages": []}
    current = "Header"
    for index, raw in enumerate(resume_entry["text"].splitlines(), 1):
        raw = raw.strip()
        heading = re.sub(r"^[▐▏▌]\s*", "", raw).strip().upper()
        if heading == "EDUCATION":
            current = "Education"
            continue
        if heading == "TECHNICAL SKILLS":
            current = "Skills"
            continue
        if heading.startswith("LANGUAGES"):
            current = "Languages"
            continue
        if heading.startswith(("KEY PROJECTS", "OTHER PROJECTS", "SELF-ASSESSMENT")):
            current = None
            continue
        if current == "Languages" and heading == "CREATIVE & MEDIA SKILLS":
            current = None
        ref = f"R{index:03d}"
        if not current or ref not in refs or not raw:
            continue
        if any(r.get("action") == "remove" and r.get("quote", "").strip()
               and r["quote"].strip() in raw for r in suggestions.get("resume_review", [])):
            continue
        if current == "Languages" and heading == "LANGUAGE PROFICIENCY":
            continue
        # Keep flat date/school/degree triples intact for the DOCX education parser.
        kind = "text"
        groups[current].append({"kind": kind, "ref": ref})
    if not groups["Header"] or not groups["Education"] or not groups["Skills"]:
        raise LLMError("模型组装失败，且底稿缺少可识别的姓名、教育或技能章节，无法安全自动组装", error_type="structure")
    sections = [{"title": "Header", "lines": groups["Header"]}]
    if "SUMMARY" in refs:
        sections.append({"title": "Summary", "lines": [{"kind": "text", "ref": "SUMMARY"}]})
    main, extra, counter, selected = [], [], 0, 0
    for index, project in enumerate(suggestions.get("projects", []), 1):
        bullets = []
        for bullet in project.get("bullets", []):
            counter += 1
            ref = f"B{counter:02d}"
            if ref in refs:
                bullets.append({"kind": "bullet", "ref": ref})
        heading = f"H{index:02d}"
        if heading not in refs or not bullets or selected >= 5:
            continue
        target, limit = (main, 3) if selected < 3 else (extra, 1)
        target.extend([{"kind": "heading", "ref": heading}, *bullets[:limit]])
        selected += 1
    if not main:
        raise LLMError("没有已验证且具有英文标题的项目，无法安全自动组装", error_type="structure")
    sections.append({"title": "Key Projects", "lines": main})
    if extra:
        sections.append({"title": "Additional Relevant Projects", "lines": extra})
    sections.extend({"title": title, "lines": groups[title]} for title in ("Education", "Skills", "Languages") if groups[title])
    return {"sections": sections}


def build(llm, suggestions, materials, resume_entry, jd_text, timeout_seconds=None, notes=None):
    by_id = {m["material_id"]: m for m in materials}
    allowed = allowed_material_ids(suggestions, materials)
    payload = compact_assembly_payload(build_payload(suggestions, materials, resume_entry, jd_text))
    refs = assembly_references(suggestions, resume_entry)
    schema = assembly_schema(refs)

    def fits():
        return llm.estimate_context("build_resume", SYSTEM_PROMPT, payload, schema) \
            <= llm.prompt_char_budget
    if not fits():
        # Shed the least essential context first: review reasons, then the JD body.
        payload["review"] = [{k: v for k, v in r.items() if k != "reason"} for r in payload["review"]]
    if not fits():
        payload["jd_text"] = jd_text[:3000] + ("…" if len(jd_text) > 3000 else "")
    if not fits():
        raise LLMError("组装简历的请求超出提示词预算，请调大 context_budget 或减少项目数",
                       error_type="context_budget")
    required = required_lines(suggestions, resume_entry["text"])
    try:
        data = llm.generate_json("build_resume", SYSTEM_PROMPT, payload, schema,
                                 timeout_seconds,
                                 validate=lambda d: validate_document(d, by_id, allowed, jd_text,
                                                                      required, refs))
    except LLMError as exc:
        if exc.error_type != "structure":
            raise
        data = assemble_verified_references(suggestions, resume_entry, refs)
        errors = validate_document(data, by_id, allowed, jd_text, required, refs)
        if errors:
            raise LLMError("程序组装未通过校验：" + "；".join(errors[:3]), error_type="structure") from exc
        note = "模型组装结构失败，已按岗位相关顺序使用已验证引用自动组装；未补写事实或修复残缺 JSON。"
        print(f"[build-resume] {note}", file=sys.stderr)
        if notes is not None:
            notes.append(note)
    place_work_experience(data, suggestions, materials, refs, notes)
    sections = []
    for section in data["sections"]:
        lines = []
        for line in section["lines"]:
            basis = []
            for b in line["basis"]:
                m = by_id[b["material_id"]]
                basis.append({**b, "kind": m["kind"], "source": m["source"],
                              "section": m["section"], "line_start": m["line_start"],
                              "line_end": m["line_end"]})
            clean = {k: v for k, v in line.items() if k != "_from_ref"}
            lines.append({**clean, "basis": basis})
        sections.append({"title": section["title"], "lines": lines})
    sections = target_headline(sections, jd_text, materials, resume_entry, notes)
    for line in sections[0]["lines"] if sections else []:
        for b in line.get("basis", []):
            if "kind" not in b:  # program-chosen headline: attach source details like other lines
                m = by_id[b["material_id"]]
                b.update(kind=m["kind"], source=m["source"], section=m["section"],
                         line_start=m["line_start"], line_end=m["line_end"])
        line.pop("_from_ref", None)
    sections = drop_self_assessment(sections, resume_entry, notes)
    sections = keep_base_skills(sections, resume_entry, notes)
    family = job_family(job_title(jd_text))
    return apply_to_sections(normalize_headings(sections), resume_entry, jd_text,
                             declared_first=family == "ai", research_last=family not in ("ai", None),
                             extra_lines=productivity_line(jd_text, materials) + networking_line(jd_text, materials))


# Where each headline term is stated, for the citation (base resume, else profile).
HEADLINE_SOURCES = {"Master of IT Graduate": "resume:Master of IT", "Data & Analytics": "resume:Data Warehousing",
                    "Requirements Analysis": "profile:requirements analysis", "Microsoft 365": "profile:Microsoft 365",
                    "Windows": "profile:Windows", "Business Intelligence": "resume:Business Intelligence",
                    "Software Development": "resume:Flask"}


def _headline_basis(headline, materials, resume_text):
    """Verbatim lines that state each headline term; None if any term lacks one."""
    basis = []
    for term in [t.strip() for t in headline.split("·")]:
        kind, _, needle = HEADLINE_SOURCES.get(term, "resume").partition(":")
        needle = needle or term
        pools = [("resume", RESUME_ID, resume_text)] if kind == "resume" else []
        pools += [("profile", m["material_id"], m["text"]) for m in materials if m["kind"] == "profile"]
        found = None
        for _, mid, text in pools:
            # Prefer the skills table rows ('◆ ...') over project tech lines that merely contain the word.
            candidates = sorted(text.splitlines(), key=lambda l: not l.strip().startswith("◆"))
            line = next((l.strip() for l in candidates
                         if re.search(r"(?<![A-Za-z])" + re.escape(needle) + r"(?![A-Za-z])", l, re.I)), None)
            if line:
                found = {"material_id": mid, "quote": line}
                break
        if found is None:
            return None
        if found not in basis:
            basis.append(found)
    return basis


def target_headline(sections, jd_text, materials, resume_entry, notes=None):
    """Replace the base headline (Header line after the name) with one for the job
    family, or restore the base headline when the assembly dropped it."""
    if not sections or sections[0]["title"].strip().upper() != "HEADER":
        return sections
    header = sections[0]["lines"]
    base_lines = [l.strip() for l in resume_entry["text"].splitlines() if l.strip()]
    if len(base_lines) < 2:
        return sections
    base = base_lines[1]
    plain = re.sub(r"^[▐▏▌]\s*", "", base)
    # Only a "Role  ·  Area" line is a headline; a section title or contact line is not.
    if "·" not in base and "|" not in base or plain.upper() == plain \
            or re.match(r"^[📍✉📞⌨☎@]|^https?:|@\S+\.", base):
        return sections
    family = job_family(job_title(jd_text))
    chosen = headline_for(family) or base
    basis = [{"material_id": RESUME_ID, "quote": base}] if chosen == base else \
        _headline_basis(chosen, materials, resume_entry["text"])
    if basis is None:
        chosen, basis = base, [{"material_id": RESUME_ID, "quote": base}]
    line = {"kind": "text", "text": chosen, "basis": basis, "_from_ref": "HEADLINE"}
    index = next((i for i, l in enumerate(header) if l.get("text", "").strip() == base), None)
    if index is None:
        index = 1 if header else 0
        header.insert(index, line)
    else:
        header[index] = line
    if notes is not None and chosen != base:
        notes.append(f"Headline targeted to job family '{family}': {chosen}")
    return sections


PRODUCTIVITY = "Productivity & Collaboration: Microsoft 365 · Excel · Word · PowerPoint · Outlook · SharePoint · Windows"
PRODUCTIVITY_JD = re.compile(r"microsoft 365|\bm365\b|office 365|microsoft office|\bms office\b|(?-i:\bExcel\b)|"
                             r"(?-i:\bOutlook\b)|sharepoint|(?-i:\bWindows\b)", re.I)


def productivity_line(jd_text, materials):
    """A user-level Microsoft 365 / Windows skills row for jobs that ask for it,
    and for IT and business-systems roles, where it is relevant unasked.

    The base resume has no such row; the profile states user-level proficiency
    (not tenant administration), so the row lists applications only."""
    family = job_family(job_title(jd_text))
    if not PRODUCTIVITY_JD.search(jd_text or "") and family not in ("it", "business"):
        return []
    basis = []
    for needle in ("Microsoft 365", "Windows"):
        for m in materials:
            if m["kind"] != "profile":
                continue
            line = next((l.strip() for l in m["text"].splitlines()
                         if needle in l and re.search(r"精通|熟练", l)), None)
            if line:
                basis.append({"material_id": m["material_id"], "quote": line, "kind": m["kind"],
                              "source": m["source"], "section": m["section"],
                              "line_start": m["line_start"], "line_end": m["line_end"]})
                break
    return [{"kind": "text", "text": PRODUCTIVITY, "basis": basis}] if len(basis) == 2 else []


NETWORKING = "Home Networking: Asuswrt-Merlin Firmware · QoS · DNS · Port Forwarding · VPN · Network Troubleshooting"
NETWORKING_JD = re.compile(r"\bnetwork|\brouter|\bwi-?fi\b|\bdns\b|\bvpn\b|\bdhcp\b|\blan\b|\bwan\b|tcp/ip", re.I)


def networking_line(jd_text, materials):
    """The profile's home-networking practice as a skills row, for IT roles or JDs
    that mention networking. Labelled 'Home Networking' so it is not read as
    enterprise network administration."""
    family = job_family(job_title(jd_text))
    if family not in ("it", "security") and not NETWORKING_JD.search(jd_text or ""):
        return []
    for m in materials:
        if m["kind"] != "profile":
            continue
        line = next((l.strip() for l in m["text"].splitlines() if "Asuswrt-Merlin" in l and "刷入" in l), None)
        if line:
            return [{"kind": "text", "text": NETWORKING, "basis": [
                {"material_id": m["material_id"], "quote": line, "kind": m["kind"], "source": m["source"],
                 "section": m["section"], "line_start": m["line_start"], "line_end": m["line_end"]}]}]
    return []


MAX_WORK_BULLETS = 3


def work_projects(materials):
    """Names of experience files marked 经历类型：工作经历 (jobs, not projects)."""
    return {m["project"] for m in materials if m.get("kind") == "experience" and WORK_TYPE.search(m.get("text", ""))}


def place_work_experience(data, suggestions, materials, refs, notes=None):
    """Put employment in its own Work Experience section after the projects, in
    place, whether the assembly filed it under Projects or left it out: a job held
    is part of every resume. Lines keep their references and citations."""
    work = work_projects(materials)
    if not work:
        return data
    heading_refs, bullet_refs, counter = {}, {}, 0
    for index, project in enumerate(suggestions.get("projects", []), 1):
        ids = []
        for _ in project.get("bullets", []):
            counter += 1
            ids.append(f"B{counter:02d}")
        if project["project"] in work and f"H{index:02d}" in refs:
            heading_refs[f"H{index:02d}"] = ids
            bullet_refs.update({b: f"H{index:02d}" for b in ids})
    if not heading_refs:
        return data
    moved = {h: [] for h in heading_refs}
    found = set()
    for section in data.get("sections", []):
        kept = []
        for line in section.get("lines", []):
            ref = line.get("_from_ref")
            if ref in heading_refs:
                found.add(ref)
                moved[ref].insert(0, line)
            elif ref in bullet_refs:
                moved[bullet_refs[ref]].append(line)
            else:
                kept.append(line)
        section["lines"] = kept
    lines = []
    for heading, ids in heading_refs.items():
        block = moved[heading]
        if heading not in found:   # left out by the assembly: heading and its first bullets
            block = [{"kind": "heading", "text": refs[heading]["text"], "basis": [dict(b) for b in refs[heading]["basis"]],
                      "_from_ref": heading}]
            block += [{"kind": "bullet", "text": refs[b]["text"], "basis": [dict(x) for x in refs[b]["basis"]],
                       "_from_ref": b} for b in ids[:MAX_WORK_BULLETS] if b in refs]
            if notes is not None:
                notes.append(f"Work experience added: {refs[heading]['text']}")
        elif block and block[0].get("_from_ref") != heading:
            block = [line for line in block if line.get("_from_ref") == heading] + \
                    [line for line in block if line.get("_from_ref") != heading]
        lines += block
    sections = [s for s in data.get("sections", []) if s.get("lines")]
    position = max((i for i, s in enumerate(sections) if re.search(r"PROJECT", s["title"], re.I)), default=None)
    position = position + 1 if position is not None else next(
        (i for i, s in enumerate(sections) if re.search(r"EDUCATION", s["title"], re.I)), len(sections))
    sections.insert(position, {"title": "Work Experience", "lines": lines})
    data["sections"] = sections
    return data


def base_skill_rows(resume_text):
    """Raw lines of the base resume's TECHNICAL SKILLS table."""
    rows, inside = [], False
    for raw in resume_text.splitlines():
        raw = raw.strip()
        if re.match(r"^[▐▏▌]", raw):
            inside = "SKILL" in raw.upper() and "LANGUAGE" not in raw.upper()
            continue
        if inside and raw:
            rows.append(raw)
    return rows


def base_section_rows(resume_text, pattern):
    """Raw lines under the base resume headings whose title matches pattern."""
    rows, inside = [], False
    for raw in resume_text.splitlines():
        raw = raw.strip()
        if re.match(r"^[▐▏▌]", raw):
            inside = bool(re.search(pattern, raw, re.I))
            continue
        if inside and raw:
            rows.append(raw)
    return rows


def drop_self_assessment(sections, resume_entry, notes=None):
    """Generic self-description from the base resume ('Strong self-learner with
    a proven ability…') is not evidence; it reached half of the resumes through
    sections titled 'Additional'. Remove it wherever it was placed."""
    filler = set(base_section_rows(resume_entry["text"], r"SELF[- ]ASSESSMENT|PERSONAL (?:STATEMENT|QUALITIES)"))
    if not filler:
        return sections
    result, removed = [], 0
    for section in sections:
        lines = [l for l in section["lines"]
                 if not (l.get("basis") and all(b.get("quote", "").strip() in filler for b in l["basis"]))]
        removed += len(section["lines"]) - len(lines)
        if lines:
            result.append({**section, "lines": lines})
    if removed and notes is not None:
        notes.append(f"Removed {removed} generic self-assessment line(s) from the base resume")
    return result


def keep_base_skills(sections, resume_entry, notes=None):
    """Supported skill rows are never silently lost: re-add base-table rows the
    assembly left out (it does in about one run in twelve). Order is set later."""
    target = next((s for s in sections if "SKILL" in s["title"].upper()
                   and "LANGUAGE" not in s["title"].upper()), None)
    if target is None:
        return sections
    cited = {b.get("quote", "").strip() for line in target["lines"] for b in line.get("basis", [])}
    added = 0
    for raw in base_skill_rows(resume_entry["text"]):
        if raw not in cited:
            target["lines"].append({"kind": "text", "text": raw, "basis": [
                {"material_id": RESUME_ID, "quote": raw,
                 **{k: resume_entry[k] for k in ("kind", "source", "section", "line_start", "line_end")}}]})
            added += 1
    if added and notes is not None:
        notes.append(f"Skills: restored {added} base-resume skill row(s) the assembly omitted")
    return sections


def _clean_heading(text):
    """'Name | type |  Group project' -> 'Name  |  Type  |  Group Project'."""
    parts, seen = [], set()
    for part in re.split(r"\s*\|\s*", text.strip()):
        part = part.strip()
        if part and part.casefold() not in seen:
            seen.add(part.casefold())
            parts.append(part[0].upper() + part[1:] if part[0].islower() else part)
    return "  |  ".join(parts)


def normalize_headings(sections):
    for section in sections:
        for line in section["lines"]:
            if line.get("kind") == "heading":
                line["text"] = _clean_heading(line["text"])
    return sections


def _project_limits(title):
    key = title.strip().upper()
    if "PROJECT" not in key and "EXPERIENCE" not in key:
        return None
    if "ADDITIONAL" in key or "OTHER" in key:
        return MAX_ADDITIONAL_PROJECTS, MAX_ADDITIONAL_BULLETS
    return MAX_KEY_PROJECTS, MAX_KEY_BULLETS


LANGUAGE_ENTRY = re.compile(r"\b(?:native|IELTS|TOEFL|PTE|fluent|bilingual|mother tongue)\b", re.I)


def _sub_label(text):
    """A short grouping label copied from the base table ('Creative & Media Skills')."""
    text = text.strip()
    return ":" not in text and len(text.split()) <= 4 and not re.search(r"[.;,]", text)


def trim_projects(sections):
    """Select a balanced one-to-two-page application view; retained facts and citations are unchanged.

    The full evidence library and suggestions remain available. Project limits
    span sections, so repeating a section cannot bypass the document budget.
    """
    notes, result = [], []
    used_projects = {"key": 0, "additional": 0}
    for section in sections:
        title = section["title"].strip().upper()
        if re.search(r"SELF[- ]ASSESSMENT|PERSONAL (?:STATEMENT|QUALITIES)|REFERENCES|HOBBIES", title):
            notes.append(f"篇幅裁剪：已略去 {section['title']}，事实证据保留在原材料")
            continue
        limits = _project_limits(title)
        if limits is None:
            lines = list(section["lines"])
            if re.search(r"ADDITIONAL|OTHER", title):
                # grouping labels copied from the base table ('Creative & Media Skills')
                lines = [l for l in lines if l["kind"] == "heading" or not _sub_label(l["text"])]
            if re.fullmatch(r"LANGUAGES?(?:\s+(?:SKILLS|PROFICIENCY))?", title):
                body = [line for line in lines if line["kind"] != "heading" and not _sub_label(line["text"])]
                language_lines = [line for line in body if LANGUAGE_ENTRY.search(line["text"])]
                if language_lines:
                    joined = {"kind": "text", "text": " | ".join(line["text"] for line in language_lines),
                              "basis": [b for line in language_lines for b in line.get("basis", [])]}
                    lines = [joined] + [line for line in body if line not in language_lines]
                else:
                    lines = body
            cap = MAX_SKILL_LINES if "SKILL" in title else None
            if cap is not None and len(lines) > cap:
                notes.append(f"篇幅提示：{section['title']}：当前 {len(lines)} 行，建议整理为约 {cap} 行；已保留原有事实")
            if lines:
                result.append({**section, "lines": lines})
            continue
        max_projects, max_bullets = limits
        is_project = "PROJECT" in title
        category = "additional" if "ADDITIONAL" in title or "OTHER" in title else "key"
        kept, projects, bullets, dropping = [], 0, 0, False
        dropped_projects, dropped_bullets = [], 0
        for line in section["lines"]:
            if line["kind"] == "heading":
                projects += 1
                bullets = 0
                dropping = projects > max_projects or (is_project and used_projects[category] >= max_projects)
                if dropping:
                    dropped_projects.append(line["text"])
                    continue
                if is_project:
                    used_projects[category] += 1
            elif dropping:
                continue
            elif line["kind"] == "bullet":
                bullets += 1
                if bullets > max_bullets:
                    dropped_bullets += 1
                    continue
            kept.append(line)
        if dropped_projects:
            notes.append(f"篇幅裁剪：{section['title']}：项目上限 {max_projects}，已略去 "
                         + "；".join(dropped_projects))
        if dropped_bullets:
            notes.append(f"篇幅裁剪：{section['title']}：每个项目最多 {max_bullets} 条要点，"
                         f"已略去 {dropped_bullets} 条")
        if kept:
            result.append({**section, "lines": kept})
    return result, notes


def render(sections, suggestions_path, resume_path, meta, notes=()):
    body, appendix = [], []
    unverified = 0
    for s_index, section in enumerate(sections, 1):
        if s_index != 1:
            body += ["", f"## {section['title']}"]
        for l_index, line in enumerate(section["lines"], 1):
            key = f"{s_index}.{l_index}"
            if s_index == 1 and l_index == 1:
                body.append(f"# {line['text']}")
            elif line["kind"] == "heading":
                body += ["", f"### {line['text']}"]
            elif line["kind"] == "bullet":
                body.append(f"- {line['text']}")
            else:
                body.append(line["text"])
            cites = []
            for b in line["basis"]:
                if b["kind"] in UNVERIFIED_KINDS:
                    unverified += 1
                if b["kind"] == "resume":
                    where = "简历底稿"
                else:
                    where = f"{b['source']} › {b['section']}（行 {b['line_start']}-{b['line_end']}）"
                cites.append(f"[{KIND_LABELS.get(b['kind'], b['kind'])}] {where}：“{b['quote']}”")
            appendix.append(f"- {key} “{line['text'][:50]}{'…' if len(line['text']) > 50 else ''}” ← "
                            + "；".join(cites))
    lines = body + ["", "---", "", "## 来源附录（发送前删除本节及以下）", "",
                    f"- 匹配运行：{meta['run_id']}；定制建议：{Path(suggestions_path).name}；"
                    f"底稿：{resume_path}；组装提示词版本 {BUILD_PROMPT_VERSION}；"
                    f"生成时间 {datetime.datetime.now().isoformat(timespec='seconds')}",
                    f"- 依据未核对材料（简历底稿、待核对经历）的引用共 {unverified} 处，其余来自已核对经历或自述事实。",
                    "- 编号为 节.行；每行的改写是否忠于引用，请逐条核对，尤其是形容词与程度副词。",
                    *[f"- {note}" for note in notes],
                    ""] + appendix
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="从匹配结果和定制建议组装完整简历（Markdown）")
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--resume", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--suggestions", type=Path, default=None,
                        help="cv_suggestions*.json，默认取运行目录里最新的一份")
    parser.add_argument("--data", type=Path, default=PROJECT_ROOT / "experiences")
    parser.add_argument("--skip-review", action="store_true", help="Skip the optional AI project-order review")
    args = parser.parse_args(argv)
    try:
        meta, matches, jd_text = load_run(args.run)
        suggestions_path = args.suggestions or latest_suggestions(args.run)
        suggestions = json.loads(Path(suggestions_path).read_text(encoding="utf-8"))
        resume_text = read_resume(args.resume)
        config = load_config(args.config)
        llm = build_llm(config)
    except (OSError, ValueError, LLMError, KeyError, zipfile.BadZipFile) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2  # unusable input, nothing written
    materials, by_project, resume_entry = load_materials(args.data, resume_text)
    stale = stale_citations(suggestions, {m["material_id"]: m for m in materials})
    if stale:
        print(f"错误：{Path(suggestions_path).name} 里有 {len(stale)} 处引用与当前经历库对不上（例如 {stale[0]}）；"
              "经历库在那之后改过，请重新运行 tailor_cv.py", file=sys.stderr)
        return 2
    print(f"[build-resume] 使用 {Path(suggestions_path).name}，材料 {len(materials)} 条；组装中……",
          file=sys.stderr)
    summary_note = ensure_summary(llm, suggestions, matches, jd_text, materials, by_project, resume_entry,
                                  config.get("timeout_seconds"))
    if summary_note:
        print(f"[build-resume] {summary_note}", file=sys.stderr)
    assembly_notes = []
    try:
        sections = build(llm, suggestions, materials, resume_entry, jd_text,
                         config.get("timeout_seconds"), notes=assembly_notes)
    except LLMError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    if args.skip_review:
        review_notes = ["AI project ordering review skipped by user."]
    else:
        print("[build-resume] Reviewing project relevance...", file=sys.stderr)
        sections, review_notes = review_projects(llm, sections, jd_text, config.get("timeout_seconds"))
    assembly_notes.extend(review_notes)
    out_md = unique_output(args.run, "resume_tailored")
    sections, trim_notes = trim_projects(sections)
    trim_notes = assembly_notes + trim_notes
    if summary_note:
        trim_notes = [summary_note, *trim_notes]
    out_md.write_text(render(sections, suggestions_path, args.resume, meta, trim_notes),
                      encoding="utf-8")
    out_md.with_suffix(".json").write_text(json.dumps(
        {"prompt_version": BUILD_PROMPT_VERSION, "run_id": meta["run_id"],
         "suggestions": str(suggestions_path), "resume_path": str(args.resume),
         "sections": sections, "assembly_notes": assembly_notes}, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(s["lines"]) for s in sections)
    print(f"已写入：{out_md}（{len(sections)} 节，{total} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
