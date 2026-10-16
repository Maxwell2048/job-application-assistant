"""Autopilot: status, run a tick now, tick history."""
import threading

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Task as TaskRow
from .deps import get_session

router = APIRouter(prefix="/api/autopilot", tags=["autopilot"])


def get_autopilot(request: Request):
    return request.app.state.autopilot


MODE = "^(focused|broad)$"


class TickIn(BaseModel):
    max_pages: int | None = None
    max_details: int | None = None
    sources: list[str] = ["inbox", "seek"]
    mode: str | None = Field(default=None, pattern=MODE)     # None: the current mode
    generate: bool | None = None                              # None: the saved choice


class ModeIn(BaseModel):
    mode: str = Field(pattern=MODE)


class GenerateIn(BaseModel):
    enabled: bool


@router.get("/status")
def status(autopilot=Depends(get_autopilot)):
    return autopilot.status()


@router.post("/mode")
def set_mode(body: ModeIn, autopilot=Depends(get_autopilot)):
    """focused: graduate/junior-style keywords; broad: every ICT job in Perth. Kept across restarts."""
    return {"mode": autopilot.set_mode(body.mode)}


@router.post("/generate")
def set_auto_generate(body: GenerateIn, autopilot=Depends(get_autopilot)):
    """true: screened jobs are queued for generation; false: they wait in the "ready" column."""
    return {"auto_generate": autopilot.set_auto_generate(body.enabled)}


@router.post("/tick", status_code=202)
def tick(body: TickIn | None = None, autopilot=Depends(get_autopilot)):
    """Start a tick in the background; watch /api/events or poll /status."""
    if autopilot.running:
        raise HTTPException(409, "上一轮自动处理仍在进行")
    body = body or TickIn()
    threading.Thread(target=autopilot.tick, kwargs={"trigger": "manual", "max_pages": body.max_pages,
                                                    "max_details": body.max_details, "sources": tuple(body.sources),
                                                    "mode": body.mode, "generate": body.generate},
                     daemon=True).start()
    return {"started": True}


@router.get("/reports")
def reports(limit: int = 20, session: Session = Depends(get_session)):
    rows = session.scalars(select(TaskRow).where(TaskRow.kind == "tick").order_by(TaskRow.created_at.desc())
                           .limit(limit)).all()
    return [row.stages[0] if row.stages else _bare_report(row) for row in rows]


def _bare_report(row):
    """A tick row without a stored report, in the shape the page expects."""
    return {"id": row.id, "trigger": "?", "started_at": row.created_at.isoformat(), "model_online": None,
            "sources": {}, "collected": 0, "new": 0, "updated": 0, "seen": 0, "repost": 0, "no_jd": 0,
            "queued": [], "flagged": [], "ready": [], "screened_out": [], "held": [], "notes": [],
            "errors": [row.error] if row.error else []}
