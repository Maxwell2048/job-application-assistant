"""Tasks: live progress, cancel, the current queue."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Task as TaskRow
from ..services.runs import run_summary
from .deps import get_session, get_settings, get_worker
from .schemas import TaskOut

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _with_run(task, outputs_dir):
    payload = task.to_dict()
    if task.run_dir and task.run_dir.is_dir():
        payload["run"] = run_summary(task.run_dir)
    return payload


@router.get("", response_model=list[TaskOut])
def list_tasks(limit: int = 50, session: Session = Depends(get_session), worker=Depends(get_worker),
               settings=Depends(get_settings)):
    """Recent tasks: live ones from memory, older ones from the table."""
    live = {t.id: _with_run(t, settings.outputs_dir) for t in worker.tasks.values()}
    # Autopilot ticks are also task rows, but their "stages" hold a report; they have their own endpoint.
    rows = session.scalars(select(TaskRow).where(TaskRow.kind != "tick").order_by(TaskRow.created_at.desc())
                           .limit(limit)).all()
    out = []
    for row in rows:
        if row.id in live:
            out.append(live.pop(row.id))
            continue
        payload = {"id": row.id, "kind": row.kind, "jd_name": "", "job_id": row.job_id,
                   "created_at": row.created_at.strftime("%Y-%m-%d %H:%M:%S"), "status": row.status,
                   "error": row.error or None, "run_id": row.run_id, "stages": row.stages or []}
        run_dir = settings.outputs_dir / row.run_id if row.run_id else None
        if run_dir is not None and run_dir.is_dir():
            payload["run"] = run_summary(run_dir)
        out.append(payload)
    return list(live.values()) + out


@router.get("/queue")
def queue_status(worker=Depends(get_worker)):
    return worker.queue_status()


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: str, session: Session = Depends(get_session), worker=Depends(get_worker),
             settings=Depends(get_settings)):
    task = worker.tasks.get(task_id)
    if task is not None:
        return _with_run(task, settings.outputs_dir)
    row = session.get(TaskRow, task_id)
    if row is None or row.kind == "tick":
        raise HTTPException(404, "任务不存在")
    payload = {"id": row.id, "kind": row.kind, "jd_name": "", "job_id": row.job_id,
               "created_at": row.created_at.strftime("%Y-%m-%d %H:%M:%S"), "status": row.status,
               "error": row.error or None, "run_id": row.run_id, "stages": row.stages or []}
    run_dir = settings.outputs_dir / row.run_id if row.run_id else None
    if run_dir is not None and run_dir.is_dir():
        payload["run"] = run_summary(run_dir)
    return payload


@router.post("/{task_id}/cancel", response_model=TaskOut, status_code=202)
def cancel(task_id: str, worker=Depends(get_worker), settings=Depends(get_settings)):
    result = worker.cancel(task_id)
    if result is None:
        raise HTTPException(404, "任务不存在")
    if result is False:
        raise HTTPException(409, "任务已经结束，无需中止")
    return _with_run(worker.tasks[task_id], settings.outputs_dir)
