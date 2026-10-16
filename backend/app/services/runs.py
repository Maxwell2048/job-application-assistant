"""Read-only helpers over a run directory (outputs/<run_id>)."""
import json
from datetime import datetime, timezone
import re
from pathlib import Path

from ..core import paths  # noqa: F401  (puts the engine on sys.path)
from export_docx import latest_docx
from tailor_cv import latest_output

RUN_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}")
DOC_NAME = re.compile(r"[A-Za-z_]+(-[0-9]+)?")
FILE_NAME = re.compile(r"[\w\-]+(\.[\w\-]+)*\.(md|docx|json|txt)")

DOCS = [("report", "匹配报告"), ("cv_suggestions", "CV 建议"),
        ("resume_tailored", "完整简历"), ("interview_prep", "面试准备")]


def run_documents(run_dir):
    """Latest Markdown per document kind plus downloadable files (only the
    newest .docx of each kind, so stale exports do not show)."""
    run_dir = Path(run_dir)
    docs = []
    for stem, label in DOCS:
        path = latest_output(run_dir, stem)
        if path is not None:
            docs.append({"stem": stem, "label": label, "file": path.name})
    downloads = [path.name for path in (latest_docx(run_dir, "resume"), latest_docx(run_dir, "cover_letter"))
                 if path is not None]
    downloads += [d["file"] for d in docs]
    return docs, downloads


def verdict_counts(run_dir):
    """direct/related/insufficient/error counts from matches.json, or {}."""
    path = Path(run_dir) / "matches.json"
    if not path.exists():
        return {}
    try:
        matches = json.loads(path.read_text(encoding="utf-8"))["matches"]
    except (OSError, ValueError, KeyError):
        return {}
    counts = {"direct": 0, "related": 0, "insufficient": 0, "error": 0}
    for m in matches:
        key = m.get("verdict") if m.get("processing_status") == "ok" else "error"
        counts[key if key in counts else "error"] += 1
    return counts


HARD_CATEGORIES = {"education", "certification", "work_authorization"}
HARD_YEARS = 3          # an experience requirement is a hard gap only from this many years up
_YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2}\s*)?\+?\s*(?:years?|yrs?|年)", re.I)


def _years_floor(requirement):
    """The lower bound of the years an experience requirement names, or 0."""
    texts = [q.get("value", "") + " " + q.get("source_quote", "") for q in requirement.get("qualifiers", [])
             if q.get("type") == "years"] + [requirement.get("text", ""), requirement.get("source_quote", "")]
    for text in texts:
        found = _YEARS.search(text or "")
        if found:
            return int(found.group(1))
    return 0


def _hard(requirement):
    if requirement.get("importance") != "required":
        return False
    category = requirement.get("category")
    return category in HARD_CATEGORIES or (category == "experience" and _years_floor(requirement) >= HARD_YEARS)


def condition_coverage(run_dir):
    """How well the evidence covers the job's conditions, or {} without matches.

    Conditions are the requirements that are not responsibilities (what the
    hire will do, not what they must bring), not preferred extras and not
    location (screened earlier). hard_gaps counts required conditions of a
    kind that cannot be learnt before applying (degree, certificates or
    licences, work rights, three or more years of experience) whose evidence
    is not direct. This is evidence coverage, not a probability of being hired."""
    run_dir = Path(run_dir)
    try:
        requirements = json.loads((run_dir / "requirements.json").read_text(encoding="utf-8"))["requirements"]
        matches = json.loads((run_dir / "matches.json").read_text(encoding="utf-8"))["matches"]
    except (OSError, ValueError, KeyError):
        return {}
    by_id = {r.get("requirement_id"): r for r in requirements}
    coverage = {"total": 0, "direct": 0, "related": 0, "insufficient": 0, "error": 0, "hard_gaps": 0}
    for match in matches:
        requirement = by_id.get(match.get("requirement_id"), {})
        category, importance = requirement.get("category"), requirement.get("importance")
        if category in ("responsibility", "location") or importance == "preferred":
            continue
        verdict = match.get("verdict") if match.get("processing_status") == "ok" else "error"
        verdict = verdict if verdict in coverage else "error"
        coverage["total"] += 1
        coverage[verdict] += 1
        if verdict != "direct" and _hard(requirement):
            coverage["hard_gaps"] += 1
    return coverage


def run_meta(run_dir):
    try:
        return json.loads((Path(run_dir) / "run_meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def run_summary(run_dir):
    """Everything the API reports about a run directory."""
    run_dir = Path(run_dir)
    docs, downloads = run_documents(run_dir)
    meta = run_meta(run_dir)
    stems = {d["stem"] for d in docs}
    try:
        generation_date = datetime.strptime(run_dir.name[:8], "%Y%m%d").date().isoformat()
    except ValueError:
        generation_date = None
    return {"run_id": run_dir.name, "run_status": meta.get("run_status"), "started_at": meta.get("started_at"),
            "jd_file": Path(str(meta.get("input", {}).get("jd_path", ""))).name,
            "counts": verdict_counts(run_dir), "coverage": condition_coverage(run_dir),
            "docs": docs, "downloads": downloads,
            "has_resume": "resume_tailored" in stems or latest_docx(run_dir, "resume") is not None,
            "generation_date": generation_date,
            "application_status": "unsubmitted", "submitted_at": None,
            "can_interview": "resume_tailored" in stems and "interview_prep" not in stems}


def safe_run_dir(outputs_dir, run_id):
    """The run directory for an id from a URL, or None when the id is not one
    of ours or the directory does not exist."""
    if not RUN_ID.fullmatch(run_id or ""):
        return None
    run_dir = Path(outputs_dir) / run_id
    return run_dir if run_dir.is_dir() else None


def safe_file(run_dir, name):
    """A file directly inside the run directory, or None."""
    if not FILE_NAME.fullmatch(name or ""):
        return None
    path = Path(run_dir) / name
    if not path.is_file() or path.resolve().parent != Path(run_dir).resolve():
        return None
    return path


def annotate_application(summary, record):
    """Return fresh status without changing the job's generation/screening state."""
    submitted = record.submitted_at if record is not None else None
    if submitted is not None and submitted.tzinfo is None:
        submitted = submitted.replace(tzinfo=timezone.utc)  # SQLite drops timezone information
    return {**summary, "application_status": "submitted" if submitted else "unsubmitted",
            "submitted_at": submitted.isoformat() if submitted else None}
