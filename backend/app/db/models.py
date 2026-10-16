"""Ledger tables.

jobs        one row per job posting the system has seen, from any source
screenings  what the eligibility screen concluded about a job, with its quote
rag_runs    one row per generation run directory under outputs/
tasks       the work queue and its history (full runs, interview prep, ticks)
"""
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_jobs_source_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))            # manual | inbox | seek
    external_id: Mapped[str] = mapped_column(String(128))     # SEEK job id, file name, or a hash
    title: Mapped[str] = mapped_column(String(300), default="")
    company: Mapped[str] = mapped_column(String(300), default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    work_type: Mapped[str] = mapped_column(String(100), default="")
    listed_at: Mapped[str] = mapped_column(String(40), default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    jd_sha256: Mapped[str] = mapped_column(String(64), default="")
    jd_path: Mapped[str] = mapped_column(String(500), default="")
    # new -> screened_out | held | queued -> processing -> done | failed | blocked | cancelled ; expired
    status: Mapped[str] = mapped_column(String(24), default="new", index=True)
    repost_of: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")

    screenings: Mapped[list["Screening"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    runs: Mapped[list["RagRun"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class Screening(Base):
    __tablename__ = "screenings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    # {exclude, reasons, flags} from the listing rules, plus "fit": the fit gate's result when it ran
    rule_result: Mapped[dict] = mapped_column(JSON, default=dict)
    # citizen_or_pr | clearance | full_rights_ok | no_sponsorship_ok | unspecified | unclear
    model_verdict: Mapped[str] = mapped_column(String(32), default="")
    quote: Mapped[str] = mapped_column(Text, default="")
    decision: Mapped[str] = mapped_column(String(16), default="")     # run | run_flagged | skip
    prompt_version: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job] = relationship(back_populates="screenings")


class RagRun(Base):
    __tablename__ = "rag_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), index=True, nullable=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True)      # outputs/<run_id>
    run_status: Mapped[str] = mapped_column(String(24), default="")
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    files: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job | None] = relationship(back_populates="runs")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))                     # full | interview | tick
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    stages: Mapped[list] = mapped_column(JSON, default=list)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResumeApplication(Base):
    """Submission belongs to a generated version, including legacy file-only runs."""
    __tablename__ = "resume_applications"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
