"""Quality report for a generated run: what to look at before sending.

    python quality_report.py --run outputs/<run>        # writes quality.json, prints 质量：…
    python quality_report.py --all                      # every run with a resume (backfill)

Program checks only; no model call. Each check is ok, warn (worth a look) or
bad (fix or regenerate before sending). The report reads the latest
cv_suggestions, resume_tailored, job_summary, the newest resume DOCX and the
run's model call log. Exit code 0 ok, 1 warnings, 2 problems.
"""
import argparse
import datetime
import json
import re
import sys
from pathlib import Path

from build_resume import base_section_rows, headline_for, job_family, job_title
from resume_skills import PREFERENCES, RESEARCH_LABELS, skill_priority
from tailor_cv import (ADMIN_CLAIM, MAX_BULLET_CHARS, RATHER_WITHOUT_THAN, internal_wording, jd_only_names,
                       latest_output, load_materials, read_resume)

PROJECT_ROOT = Path(__file__).resolve().parent
LEVELS = {"ok": 0, "warn": 1, "bad": 2}
TWO_PAGE_WORDS = 1050          # 11 pt layout v6: about 450-520 words a page
LEVEL_LABELS = {"ok": "通过", "warn": "提醒", "bad": "问题"}


def _load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")) if path else None
    except (OSError, ValueError):
        return None


def _docx_paragraphs(run_dir):
    files = sorted((p for p in Path(run_dir).glob("*.docx") if "Resume" in p.name or p.name.startswith("resume")),
                   key=lambda p: p.stat().st_mtime)
    if not files:
        return None, []
    try:
        import docx
        return files[-1].name, [p.text for p in docx.Document(files[-1]).paragraphs if p.text.strip()]
    except Exception:  # noqa: BLE001 - an unreadable file is reported, not raised
        return files[-1].name, []


def call_stats(run_dir):
    """Totals from llm_calls.jsonl: calls, seconds, retried and failed requests, slowest tasks."""
    path = Path(run_dir) / "llm_calls.jsonl"
    if not path.is_file():
        return {}
    calls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            calls.append(json.loads(line))
        except ValueError:
            continue
    if not calls:
        return {}
    by_task = {}
    for c in calls:
        name = re.sub(r"[:_]\d+$|:.*$", "", c.get("task", "?"))
        entry = by_task.setdefault(name, {"calls": 0, "seconds": 0.0, "retries": 0, "failed": 0})
        entry["calls"] += 1
        entry["seconds"] += c.get("seconds") or 0
        entry["retries"] += max(0, (c.get("attempts") or 1) - 1)
        entry["failed"] += c.get("outcome") != "ok"
    failures = {}
    for c in calls:
        for f in c.get("failures") or []:
            failures[f.get("type", "?")] = failures.get(f.get("type", "?"), 0) + 1
    return {"calls": len(calls), "seconds": round(sum(c.get("seconds") or 0 for c in calls), 1),
            "retries": sum(max(0, (c.get("attempts") or 1) - 1) for c in calls),
            "failed": sum(c.get("outcome") != "ok" for c in calls),
            "prompt_chars": sum(c.get("prompt_chars") or 0 for c in calls),
            "failure_types": failures,
            "by_task": {k: {**v, "seconds": round(v["seconds"], 1)}
                        for k, v in sorted(by_task.items(), key=lambda kv: -kv[1]["seconds"])}}


def assess(run_dir, materials=None, resume_text=None):
    run_dir = Path(run_dir)
    checks = []

    def add(key, label, level, detail=""):
        checks.append({"key": key, "label": label, "level": level, "detail": detail})

    jd = (run_dir / "jd.txt").read_text(encoding="utf-8") if (run_dir / "jd.txt").is_file() else ""
    title = job_title(jd)
    family = job_family(title)
    suggestions = _load(latest_output(run_dir, "cv_suggestions", ".json"))
    resume = _load(latest_output(run_dir, "resume_tailored", ".json"))
    summary_file = _load(run_dir / "job_summary.json")
    if materials is None:
        materials, _, entry = load_materials(PROJECT_ROOT / "experiences",
                                             resume_text if resume_text is not None else
                                             read_resume(PROJECT_ROOT / "resume" / "base_resume.docx"))
        resume_text = entry["text"]
    by_id = {m["material_id"]: m for m in materials}

    if not resume:
        add("resume", "完整简历", "bad", "没有 resume_tailored：生成未完成")
        return _finish(run_dir, checks, family, title)

    # -- generation completeness
    if suggestions:
        lost = [e["step"].split(":", 1)[1] for e in suggestions.get("errors", []) if e["step"].startswith("bullets:")]
        order = suggestions.get("project_order", [])
        top = {p["project"] for p in order[:4]}
        if lost:
            add("projects", "项目要点", "bad" if top.intersection(lost) else "warn",
                f"{len(lost)} 个项目要点生成失败：{'、'.join(lost)}" + ("（含前四个最相关项目）" if top.intersection(lost) else ""))
        else:
            add("projects", "项目要点", "ok", f"{len(suggestions.get('projects', []))} 个项目全部生成")
        if not suggestions.get("summary"):
            add("summary", "简历摘要", "bad", "没有生成摘要")
        letter = suggestions.get("cover_letter") or []
        add("cover_letter", "求职信", "ok" if letter else "bad", f"{len(letter)} 段" if letter else "没有生成求职信")
    if summary_file is not None:
        ok = summary_file.get("status") == "complete"
        add("readiness", "求职准备总结", "ok" if ok else "warn",
            f"程序纠正 {len(summary_file.get('normalized', []))} 处" if ok else (summary_file.get("error") or "")[:160])

    # -- wording in what a recruiter reads
    problems = []
    for section in resume["sections"]:
        for line in section["lines"]:
            text = line.get("text", "")
            if section["title"] != "Summary" and line.get("kind") != "bullet":
                continue
            for label, hit in (("内部核对用语", internal_wording(text)),
                               ("管理员侧或终端用户支持", (ADMIN_CLAIM.search(text) or [None])[0]),
                               ("rather 缺少 than", (RATHER_WITHOUT_THAN.search(text) or [None])[0]),
                               ("只在 JD 中出现", "、".join(jd_only_names(text, jd, by_id)))):
                if hit:
                    problems.append(f"{label}：{hit}")
    add("wording", "措辞", "bad" if problems else "ok", "；".join(problems[:6]) or "未发现内部用语、越界声明或照搬 JD 的名称")

    filler = set(base_section_rows(resume_text or "", r"SELF[- ]ASSESSMENT|PERSONAL (?:STATEMENT|QUALITIES)"))
    filler_lines = [l["text"][:40] for s in resume["sections"] for l in s["lines"]
                    if l.get("basis") and all(b.get("quote", "").strip() in filler for b in l["basis"])]
    if filler_lines:
        add("filler", "自我评价套话", "warn", "；".join(filler_lines))

    # -- headline and skills
    header = resume["sections"][0]["lines"] if resume["sections"] else []
    headline = header[1]["text"] if len(header) > 1 else ""
    expected = headline_for(family) if family else None
    if expected and headline != expected:
        add("headline", "抬头", "warn", f"岗位方向是 {family}，抬头却是 “{headline}”")
    else:
        add("headline", "抬头", "ok", headline)
    skill_lines = [l["text"] for s in resume["sections"] if "SKILL" in s["title"].upper()
                   and "LANGUAGE" not in s["title"].upper() for l in s["lines"]]
    items, duplicates = {}, set()
    for text in skill_lines:
        for item in re.split(r"\s*[·,]\s*", text.split(":", 1)[-1]):
            key = item.strip().casefold()
            if key and key in items and items[key] != text:
                duplicates.add(item.strip())
            items.setdefault(key, text)
    skill_notes = []
    if duplicates:
        skill_notes.append("重复：" + "、".join(sorted(duplicates)[:6]))
    if skill_lines and family not in ("ai", None):
        first = skill_lines[0].split(":", 1)[0].strip().casefold()
        if first in RESEARCH_LABELS or skill_priority({"text": skill_lines[0]}) < len(PREFERENCES):
            skill_notes.append(f"非 AI 岗位，技能第一行是 {skill_lines[0].split(':', 1)[0]}")
    add("skills", "技能", "warn" if skill_notes else "ok", "；".join(skill_notes) or f"{len(skill_lines)} 行")

    # -- length, from the exported document
    name, paragraphs = _docx_paragraphs(run_dir)
    if name is None:
        add("docx", "Word 文件", "bad", "没有导出简历 Word")
    else:
        words = sum(len(p.split()) for p in paragraphs)
        long = [l["text"][:40] for s in resume["sections"] for l in s["lines"]
                if l.get("kind") == "bullet" and len(l.get("text", "")) > MAX_BULLET_CHARS]
        level = "warn" if words > TWO_PAGE_WORDS or long else "ok"
        detail = f"约 {words} 词（{'可能超过两页' if words > TWO_PAGE_WORDS else '一到两页'}）"
        if long:
            detail += f"；{len(long)} 条要点超过 {MAX_BULLET_CHARS} 字符"
        add("length", "篇幅", level, detail)
        if paragraphs and "SKILLS" in paragraphs and skill_lines:
            shown = paragraphs[paragraphs.index("SKILLS") + 1].split(":", 1)[0].strip()
            if shown != skill_lines[0].split(":", 1)[0].strip():
                add("export", "导出顺序", "warn", f"Word 里技能第一行是 {shown}，与简历顺序不一致；请重新导出")
    return _finish(run_dir, checks, family, title)


def _finish(run_dir, checks, family, title):
    from_runs = None
    try:  # evidence coverage, as the board shows it
        sys.path.insert(0, str(PROJECT_ROOT))
        from backend.app.services.runs import condition_coverage
        from_runs = condition_coverage(run_dir)
    except Exception:  # noqa: BLE001 - optional when the backend package is absent
        from_runs = None
    level = max((c["level"] for c in checks), key=LEVELS.get, default="ok")
    issues = sum(c["level"] != "ok" for c in checks)
    return {"schema_version": "1.0", "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "job_title": title, "job_family": family, "level": level, "issues": issues, "checks": checks,
            "coverage": from_runs or {}, "llm": call_stats(run_dir)}


def headline(report):
    if report["level"] == "ok":
        return "通过"
    bad = sum(c["level"] == "bad" for c in report["checks"])
    warn = sum(c["level"] == "warn" for c in report["checks"])
    return "；".join(x for x in (f"{bad} 项问题" if bad else "", f"{warn} 项提醒" if warn else "") if x)


def write(run_dir, report):
    (Path(run_dir) / "quality.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成结果的质量检查（不调用模型）")
    parser.add_argument("--run", type=Path)
    parser.add_argument("--all", action="store_true", help="检查 outputs 下所有有简历的运行")
    parser.add_argument("--outputs", type=Path, default=PROJECT_ROOT / "outputs")
    parser.add_argument("--resume", type=Path, default=PROJECT_ROOT / "resume" / "base_resume.docx")
    parser.add_argument("--data", type=Path, default=PROJECT_ROOT / "experiences")
    args = parser.parse_args(argv)
    if not args.run and not args.all:
        parser.error("需要 --run 或 --all")
    resume_text = read_resume(args.resume)
    materials, _, entry = load_materials(args.data, resume_text)
    runs = [args.run] if args.run else [d for d in sorted(args.outputs.iterdir())
                                        if d.is_dir() and latest_output(d, "resume_tailored", ".json")]
    worst = 0
    for run_dir in runs:
        report = assess(run_dir, materials, entry["text"])
        write(run_dir, report)
        worst = max(worst, LEVELS[report["level"]])
        print(f"{Path(run_dir).name} 质量：{headline(report)}")
    return worst


if __name__ == "__main__":
    sys.exit(main())
