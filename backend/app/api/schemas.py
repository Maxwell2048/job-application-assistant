"""Request and response models."""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class HealthOut(BaseModel):
    online: bool
    model: str | None = None
    queue: dict[str, Any]
    autopilot_enabled: bool


class SubmitJobIn(BaseModel):
    jd_text: str = Field(min_length=1)
    jd_name: str = ""
    with_interview: bool = False
    # screen first, like a collected job: eligible jobs then follow the autopilot page's choice
    # (queued for generation, or listed as "ready"); false: generate straight away
    screen_first: bool = False


class SupplementJdIn(BaseModel):
    jd_text: str = Field(min_length=1)


class ScreeningStartedOut(BaseModel):
    job_id: int
    status: str
    screening: str      # started | busy (a round is running; the next one screens it) | repost


class DecisionIn(BaseModel):
    action: str = Field(pattern="^(run|skip|rerun)$")
    with_interview: bool = False


class StageOut(BaseModel):
    key: str
    label: str
    status: str
    seconds: float | None = None
    message: str = ""


class TaskOut(BaseModel):
    id: str
    kind: str
    jd_name: str
    job_id: int | None
    created_at: str
    status: str
    error: str | None
    run_id: str | None
    stages: list[StageOut]
    run: dict[str, Any] | None = None


class ScreeningOut(BaseModel):
    model_verdict: str
    quote: str
    decision: str
    rule_result: dict[str, Any]
    created_at: datetime


class JobOut(BaseModel):
    id: int
    source: str
    external_id: str
    title: str
    company: str
    url: str
    location: str
    work_type: str
    listed_at: str
    first_seen: datetime
    last_seen: datetime
    status: str
    repost_of: int | None
    note: str
    jd_path: str
    screening: ScreeningOut | None = None
    runs: list[dict[str, Any]] = []


class JobDetailOut(JobOut):
    jd_text: str = ""
    tasks: list[dict[str, Any]] = []


class SettingsOut(BaseModel):
    resume_path: str
    outputs_dir: str
    jobs_dir: str
    database: str
    autopilot_enabled: bool
    autopilot_interval_hours: float
    collector_dir: str | None
    model_launcher: str | None
