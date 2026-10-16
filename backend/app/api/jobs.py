"""Jobs: the board, one job's detail, manual submission, decisions."""
import hashlib
from pathlib import Path

import threading

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from ..core import paths  # noqa: F401
import add_jd
from ..db.models import Job, RagRun, Screening, Task as TaskRow, ResumeApplication
from ..services.ledger import ingest
from ..services.runs import run_summary, annotate_application
from .deps import get_session, get_settings, get_worker
from .schemas import DecisionIn, JobDetailOut, JobOut, ScreeningStartedOut, SubmitJobIn, SupplementJdIn, TaskOut

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _screening(job):
    latest = max(job.screenings, key=lambda s: s.created_at, default=None)
    if latest is None:
        return None
    return {"model_verdict": latest.model_verdict, "quote": latest.quote, "decision": latest.decision,
            "rule_result": latest.rule_result or {}, "created_at": latest.created_at}


def _runs(job, outputs_dir):
    out = []
    for run in sorted(job.runs, key=lambda r: r.created_at, reverse=True):
        run_dir = Path(outputs_dir) / run.run_id
        summary = (run_summary(run_dir) if run_dir.is_dir() else
                   {"run_id": run.run_id, "run_status": run.run_status, "counts": run.counts, "docs": [],
                    "downloads": run.files, "can_interview": False, "has_resume": False, "missing": True})
        session = object_session(job)
        out.append(annotate_application(summary, session.get(ResumeApplication, run.run_id) if session else None))
    return out


def job_out(job, outputs_dir):
    return JobOut(id=job.id, source=job.source, external_id=job.external_id, title=job.title, company=job.company,
                  url=job.url, location=job.location, work_type=job.work_type, listed_at=job.listed_at,
                  first_seen=job.first_seen, last_seen=job.last_seen, status=job.status, repost_of=job.repost_of,
                  note=job.note, jd_path=job.jd_path, screening=_screening(job), runs=_runs(job, outputs_dir))


@router.get("", response_model=list[JobOut])
def list_jobs(status: str | None = Query(default=None), limit: int = Query(default=200, ge=1, le=1000), offset: int = Query(default=0, ge=0),
              session: Session = Depends(get_session), settings=Depends(get_settings)):
    query = select(Job).order_by(Job.last_seen.desc(), Job.id.desc()).offset(offset).limit(limit)
    if status:
        query = query.where(Job.status.in_(status.split(",")))
    return [job_out(job, settings.outputs_dir) for job in session.scalars(query).all()]


@router.post("/generate-ready", status_code=202)
def generate_ready(worker=Depends(get_worker)):
    """Queue every job waiting in the "ready" column, best fit first."""
    online, model = worker.model_online()
    if not online:
        raise HTTPException(503, f"本机模型服务不在线：{model}")
    queued = worker.generate_ready()
    worker.events.publish("jobs", {"queued": queued})
    return {"queued": queued}


@router.get("/{job_id}", response_model=JobDetailOut)
def get_job(job_id: int, session: Session = Depends(get_session), settings=Depends(get_settings)):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "岗位不存在")
    base = job_out(job, settings.outputs_dir).model_dump()
    jd_text = ""
    if job.jd_path:
        try:
            jd_text = Path(job.jd_path).read_text(encoding="utf-8")
        except OSError:
            jd_text = ""
    tasks = session.scalars(select(TaskRow).where(TaskRow.job_id == job.id).order_by(TaskRow.created_at.desc())).all()
    base["tasks"] = [{"id": t.id, "kind": t.kind, "status": t.status, "run_id": t.run_id, "error": t.error,
                      "created_at": t.created_at.isoformat(), "stages": t.stages} for t in tasks]
    return JobDetailOut(**base, jd_text=jd_text)


def _screen_soon(request, trigger):
    """Screen the waiting jobs now, in the background, as a round that collects
    nothing. -> started | busy (a round is running; the next one screens them)."""
    autopilot = request.app.state.autopilot
    if autopilot.running:
        return "busy"
    threading.Thread(target=autopilot.tick, kwargs={"trigger": trigger, "sources": ()}, daemon=True).start()
    return "started"


def _pasted_jd(job, jd_text):
    """A pasted JD with the listing facts in front, like the collector writes them,
    so later stages can read the company and title."""
    facts = [("Title", job.title), ("Company", job.company), ("Listing location", job.location),
             ("Work type", job.work_type), ("Source URL", job.url)]
    header = "\n".join(f"{label}: {value}" for label, value in facts if value)
    return ("[Listing facts — JD pasted by hand]\n" + header + "\n\n[Employer job description]\n" + jd_text
            if header else jd_text)


@router.post("/{job_id}/jd", response_model=ScreeningStartedOut, status_code=202)
def supplement_jd(job_id: int, body: SupplementJdIn, request: Request, session: Session = Depends(get_session),
                  settings=Depends(get_settings), worker=Depends(get_worker)):
    """Paste the JD of a job known only by its title (SEEK would not serve the
    detail page). It is recorded like a collected JD and screened right away."""
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "岗位不存在")
    jd_text = body.jd_text.strip()
    if len(jd_text) < add_jd.MIN_CHARS:
        raise HTTPException(400, f"JD 文本太短（少于 {add_jd.MIN_CHARS} 字符）")
    if job.jd_sha256:
        raise HTTPException(409, "这个岗位已经有 JD")
    if job.status in ("queued", "processing"):
        raise HTTPException(409, "岗位正在队列或处理中")
    job.note = (job.note + "\n" if job.note else "") + "JD 为手动粘贴"
    job, outcome = ingest(session, {"source": job.source, "external_id": job.external_id,
                                    "jd_text": _pasted_jd(job, jd_text)}, settings.jobs_dir)
    job.status = job.status if outcome == "repost" else "new"
    session.commit()
    worker.events.publish("jobs", {"id": job.id, "status": job.status})
    screening = "repost" if outcome == "repost" else _screen_soon(request, "补充 JD")
    return ScreeningStartedOut(job_id=job.id, status=job.status, screening=screening)


@router.post("", response_model=TaskOut | ScreeningStartedOut, status_code=202)
def submit_job(body: SubmitJobIn, request: Request, session: Session = Depends(get_session),
               worker=Depends(get_worker), settings=Depends(get_settings)):
    """Paste a JD: it becomes a job of source "manual" and a full task, or, with
    screen_first, a job screened like a collected one."""
    jd_text = body.jd_text.strip()
    if len(jd_text) < add_jd.MIN_CHARS:
        raise HTTPException(400, f"JD 文本太短（少于 {add_jd.MIN_CHARS} 字符）")
    if body.screen_first:
        digest = hashlib.sha256(jd_text.encode("utf-8")).hexdigest()
        title = body.jd_name.strip() or next((line.strip() for line in jd_text.splitlines() if line.strip()), "")
        existing = session.scalar(select(Job).where(Job.source == "manual", Job.external_id == digest[:32]))
        if existing is not None and existing.status not in ("new", "held", "no_jd"):
            raise HTTPException(409, f"这份 JD 已经提交过（状态：{existing.status}），在看板里打开它")
        job, outcome = ingest(session, {"source": "manual", "external_id": digest[:32], "title": title[:300],
                                        "jd_text": jd_text}, settings.jobs_dir)
        session.commit()
        worker.events.publish("jobs", {"id": job.id, "status": job.status})
        screening = "repost" if outcome == "repost" else _screen_soon(request, "手动提交")
        return ScreeningStartedOut(job_id=job.id, status=job.status, screening=screening)
    online, model = worker.model_online()
    if not online:
        raise HTTPException(503, f"本机模型服务不在线：{model}")
    digest = hashlib.sha256(jd_text.encode("utf-8")).hexdigest()
    title = next((line.strip() for line in jd_text.splitlines() if line.strip()), "")[:300]
    # The same text pasted again is the same job: reuse its row instead of a duplicate.
    job = session.scalar(select(Job).where(Job.source == "manual", Job.external_id == digest[:32]))
    if job is not None and job.status in ("queued", "processing"):
        raise HTTPException(409, "这份 JD 已在队列或处理中")
    if job is None:
        job = Job(source="manual", external_id=digest[:32], jd_sha256=digest)
        session.add(job)
    job.title = body.jd_name.strip() or title
    job.status = "queued"
    session.commit()
    task = worker.submit_full(jd_text=jd_text, jd_name=body.jd_name.strip(), with_interview=body.with_interview,
                              job_id=job.id)
    return TaskOut(**task.to_dict())


@router.post("/{job_id}/decision", response_model=TaskOut | JobOut, status_code=202)
def decide(job_id: int, body: DecisionIn, session: Session = Depends(get_session), worker=Depends(get_worker),
           settings=Depends(get_settings)):
    """run / rerun: queue a full generation for a job whose JD is on disk.
    skip: mark the job as skipped by hand."""
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "岗位不存在")
    if body.action == "skip":
        if job.status in ("processing",):
            raise HTTPException(409, "岗位正在处理，先中止任务")
        job.status = "screened_out"
        job.note = (job.note + "\n" if job.note else "") + "手动跳过"
        session.commit()
        worker.events.publish("jobs", {"id": job.id, "status": job.status})   # every open board refreshes
        return job_out(job, settings.outputs_dir)
    if not job.jd_path or not Path(job.jd_path).is_file():
        raise HTTPException(400, "这个岗位没有 JD 文件，无法生成")
    if job.status in ("queued", "processing"):
        raise HTTPException(409, "岗位已在队列或处理中")
    online, model = worker.model_online()
    if not online:
        raise HTTPException(503, f"本机模型服务不在线：{model}")
    job.status = "queued"
    session.commit()
    task = worker.submit_full(jd_name=job.company or job.title, with_interview=body.with_interview, job_id=job.id,
                              jd_path=Path(job.jd_path))
    return TaskOut(**task.to_dict())


@router.post("/{job_id}/interview", response_model=TaskOut, status_code=202)
def interview(job_id: int, session: Session = Depends(get_session), worker=Depends(get_worker),
              settings=Depends(get_settings)):
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "岗位不存在")
    run = max(job.runs, key=lambda r: r.created_at, default=None)
    run_dir = Path(settings.outputs_dir) / run.run_id if run else None
    if run_dir is None or not run_dir.is_dir():
        raise HTTPException(400, "这个岗位还没有生成结果")
    online, model = worker.model_online()
    if not online:
        raise HTTPException(503, f"本机模型服务不在线：{model}")
    task = worker.submit_interview(run_dir, job_id=job.id)
    return TaskOut(**task.to_dict())
