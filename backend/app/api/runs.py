"""Run directories: list, documents as HTML fragments, downloads."""
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, StrictBool
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from fastapi.responses import FileResponse

from ..core import paths  # noqa: F401
import build_site
from job_identity import load_identity
from tailor_cv import latest_output
from ..services.runs import DOC_NAME, annotate_application, run_summary, safe_file, safe_run_dir
from ..db.models import ResumeApplication, RagRun, Job
from ..services.run_index import scan_run_dir
from .deps import get_run_index, get_settings, get_session, get_worker

router = APIRouter(prefix="/api/runs", tags=["runs"])


def _run_dir(settings, run_id):
    run_dir = safe_run_dir(settings.outputs_dir, run_id)
    if run_dir is None:
        raise HTTPException(404, "运行不存在")
    return run_dir


@router.get("")
def list_runs(limit: int = Query(default=1000, ge=1, le=5000), resumes_only: bool = False,
              settings=Depends(get_settings), session: Session = Depends(get_session),
              run_index=Depends(get_run_index)):
    # One scandir per run directory both finds run_meta.json and signs the
    # directory for the run index, which rebuilds only the summaries that changed.
    outputs = Path(settings.outputs_dir)
    listings = {}
    if outputs.is_dir():
        with os.scandir(outputs) as entries:
            dirs = sorted((entry.name for entry in entries if entry.is_dir()), reverse=True)
        for name in dirs:
            listing = scan_run_dir(outputs / name)
            if listing is not None and "run_meta.json" in listing.names:
                listings[name] = listing
    summaries = run_index.summaries((outputs / name for name in listings), listings)
    runs = ({"dir": outputs / name} for name in listings)
    applications = {r.run_id: r for r in session.scalars(select(ResumeApplication)).all()}
    jobs = {r.run_id: j for r, j in session.execute(select(RagRun, Job).join(Job, RagRun.job_id == Job.id).options(selectinload(Job.screenings))).all()}
    # Resolve submission across the whole ledger before applying the response
    # limit. Keep the actual per-version markers unchanged.
    submitted_jobs = {}
    for run_id, job in jobs.items():
        application = applications.get(run_id)
        if application and application.submitted_at:
            previous = submitted_jobs.get(job.id)
            if previous is None or (application.submitted_at, run_id) > (previous.submitted_at, previous.run_id):
                submitted_jobs[job.id] = application
    results = []
    for run in runs:
        summary = summaries[run["dir"].name]
        if resumes_only and not summary["has_resume"]:
            continue
        job = jobs.get(summary["run_id"])
        identity = load_identity(run["dir"]) if job is None else {}
        latest_screen = max(job.screenings, key=lambda row: (row.created_at, row.id), default=None) if job else None
        archived = bool(job and (job.status in ("screened_out", "expired", "blocked") or (latest_screen and latest_screen.decision == "skip" and job.status not in ("queued", "processing"))))
        summary.update(archived=archived, job_status=job.status if job else None, job_id=job.id if job else None, title=job.title if job else identity.get("job_title") or summary["jd_file"],
                       company=job.company if job else identity.get("company", ""))
        job_submission = submitted_jobs.get(job.id) if job else None
        summary.update(job_application_status=("submitted" if job_submission else "unsubmitted") if job else None,
                       job_submitted_run_id=job_submission.run_id if job_submission else None,
                       job_submitted_at=annotate_application({}, job_submission)["submitted_at"])
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


class RedoIn(BaseModel):
    step: Literal["tailor", "closing", "assemble", "export"]


REDO_NEEDS = {"tailor": "matches.json", "closing": "cv_suggestions", "assemble": "cv_suggestions",
              "export": "resume_tailored"}


@router.post("/{run_id}/redo", status_code=202)
def redo(run_id: str, body: RedoIn, settings=Depends(get_settings), session: Session = Depends(get_session),
         worker=Depends(get_worker)):
    """Repeat later stages of a run without matching again: tailor (continue a run the
    coverage gate stopped), closing (summary and cover letter), assemble, export."""
    run_dir = _run_dir(settings, run_id)
    need = REDO_NEEDS[body.step]
    if not (run_dir / need).is_file() and latest_output(run_dir, need, ".json") is None:
        raise HTTPException(409, f"这次运行缺少 {need}，不能从这一步重做")
    if any(t.run_dir and t.run_dir.name == run_id and t.status in ("queued", "running")
           for t in worker.tasks.values()):
        raise HTTPException(409, "这次运行已有任务在队列或处理中")
    if body.step != "export":
        online, model = worker.model_online()
        if not online:
            raise HTTPException(503, f"本机模型服务不在线：{model}")
    run = session.scalar(select(RagRun).where(RagRun.run_id == run_id))
    job = session.get(Job, run.job_id) if run is not None and run.job_id is not None else None
    name = (job.company or job.title) if job is not None else load_identity(run_dir).get("company", "")
    task = worker.submit_redo(run_dir, body.step, job_id=job.id if job is not None else None, jd_name=name or "")
    return task.to_dict()


class AnswerIn(BaseModel):
    question: str = Field(min_length=5, max_length=1000)
    max_words: int = Field(default=250, ge=40, le=600)


@router.post("/{run_id}/answers", status_code=202)
def answer(run_id: str, body: AnswerIn, settings=Depends(get_settings), session: Session = Depends(get_session),
           worker=Depends(get_worker)):
    """Queue an answer to an application-form question, written from this run's material."""
    run_dir = _run_dir(settings, run_id)
    if not (run_dir / "matches.json").is_file():
        raise HTTPException(409, "这次运行没有匹配结果，不能回答申请问题")
    online, model = worker.model_online()
    if not online:
        raise HTTPException(503, f"本机模型服务不在线：{model}")
    run = session.scalar(select(RagRun).where(RagRun.run_id == run_id))
    job = session.get(Job, run.job_id) if run is not None and run.job_id is not None else None
    task = worker.submit_answer(run_dir, body.question.strip(), body.max_words,
                                job_id=job.id if job is not None else None,
                                jd_name=(job.company or job.title) if job is not None else "")
    return task.to_dict()


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
