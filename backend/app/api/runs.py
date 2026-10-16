"""Run directories: list, documents as HTML fragments, downloads."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, StrictBool
from sqlalchemy import select
from sqlalchemy.orm import Session
from fastapi.responses import FileResponse

from ..core import paths  # noqa: F401
import build_site
from job_identity import load_identity
from ..services.runs import DOC_NAME, annotate_application, run_summary, safe_file, safe_run_dir
from ..db.models import ResumeApplication, RagRun, Job
from .deps import get_settings, get_session, get_worker

router = APIRouter(prefix="/api/runs", tags=["runs"])


def _run_dir(settings, run_id):
    run_dir = safe_run_dir(settings.outputs_dir, run_id)
    if run_dir is None:
        raise HTTPException(404, "运行不存在")
    return run_dir


@router.get("")
def list_runs(limit: int = Query(default=1000, ge=1, le=5000), resumes_only: bool = False,
              settings=Depends(get_settings), session: Session = Depends(get_session)):
    runs = build_site.scan_runs(settings.outputs_dir)
    applications = {r.run_id: r for r in session.scalars(select(ResumeApplication)).all()}
    jobs = {r.run_id: j for r, j in session.execute(select(RagRun, Job).join(Job, RagRun.job_id == Job.id)).all()}
    results = []
    for run in runs:
        summary = run_summary(run["dir"])
        if resumes_only and not summary["has_resume"]:
            continue
        job = jobs.get(summary["run_id"])
        identity = load_identity(run["dir"]) if job is None else {}
        latest_screen = max(job.screenings, key=lambda row: (row.created_at, row.id), default=None) if job else None
        archived = bool(job and (job.status in ("screened_out", "expired", "blocked") or (latest_screen and latest_screen.decision == "skip" and job.status not in ("queued", "processing"))))
        summary.update(archived=archived, job_status=job.status if job else None, job_id=job.id if job else None, title=job.title if job else identity.get("job_title") or summary["jd_file"],
                       company=job.company if job else identity.get("company", ""))
        results.append(annotate_application(summary, applications.get(summary["run_id"])))
        if len(results) >= limit:
            break
    return results


@router.get("/{run_id}")
def get_run(run_id: str, settings=Depends(get_settings), session: Session = Depends(get_session)):
    return annotate_application(run_summary(_run_dir(settings, run_id)), session.get(ResumeApplication, run_id))


@router.get("/{run_id}/docs/{name}")
def get_doc(run_id: str, name: str, settings=Depends(get_settings)):
    """A Markdown document rendered to an HTML fragment plus its raw text."""
    run_dir = _run_dir(settings, run_id)
    if not DOC_NAME.fullmatch(name):
        raise HTTPException(404, "文档不存在")
    path = run_dir / f"{name}.md"
    if not path.is_file():
        raise HTTPException(404, "文档不存在")
    markdown = path.read_text(encoding="utf-8")
    return {"run_id": run_id, "name": name, "markdown": markdown, "html": build_site.md_to_html(markdown)}


@router.get("/{run_id}/download/{name}")
def download(run_id: str, name: str, settings=Depends(get_settings)):
    run_dir = _run_dir(settings, run_id)
    path = safe_file(run_dir, name)
    if path is None:
        raise HTTPException(404, "文件不存在")
    return FileResponse(path, filename=path.name)


class ApplicationIn(BaseModel):
    submitted: StrictBool


@router.put("/{run_id}/application")
def set_application(run_id: str, body: ApplicationIn, settings=Depends(get_settings),
                    session: Session = Depends(get_session), worker=Depends(get_worker)):
    summary = run_summary(_run_dir(settings, run_id))
    if not summary["has_resume"]:
        raise HTTPException(409, "这次运行还没有生成完整简历，无法标记投递")
    record = session.get(ResumeApplication, run_id)
    now = datetime.now(timezone.utc)
    if record is None:
        record = ResumeApplication(run_id=run_id)
        session.add(record)
    if body.submitted:
        record.submitted_at = record.submitted_at or now
    else:
        record.submitted_at = None
    record.updated_at = now
    session.commit()
    worker.events.publish("jobs", {"run_id": run_id, "application_status": "submitted" if body.submitted else "unsubmitted"})
    return annotate_application(summary, record)
