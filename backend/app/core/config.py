"""Backend settings. Every value can be overridden by an environment variable
with the RAG_ prefix (RAG_DATABASE_URL, RAG_OUTPUTS_DIR, ...) or by a .env
file in the repository root, which is how compose.yaml points the container
at mounted directories and PostgreSQL and how the private SEEK collector is
plugged in without appearing in the public code."""
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .paths import PROJECT_ROOT


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RAG_", env_file=str(PROJECT_ROOT / ".env"),
                                      env_file_encoding="utf-8", extra="ignore")

    project_root: Path = PROJECT_ROOT
    jobs_dir: Path = PROJECT_ROOT / "jobs"
    inbox_dir: Path = PROJECT_ROOT / "jobs" / "inbox"     # drop JD text files here; the tick picks them up
    outputs_dir: Path = PROJECT_ROOT / "outputs"
    resume_path: Path = PROJECT_ROOT / "resume" / "base_resume.docx"
    config_path: Path = PROJECT_ROOT / "config.local.json"
    frontend_dist: Path = PROJECT_ROOT / "frontend" / "dist"

    # SQLite by default (development, tests, single-machine use); PostgreSQL under Compose.
    database_url: str = f"sqlite:///{(PROJECT_ROOT / 'state' / 'rag.sqlite').as_posix()}"
    # Create tables on start-up when no Alembic migration has run (SQLite, tests).
    auto_create_schema: bool = True

    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://127.0.0.1:5173", "http://localhost:5173"])

    # -- autopilot ------------------------------------------------------------
    autopilot_enabled: bool = False
    autopilot_interval_hours: float = 6.0
    collector_dir: Path | None = None          # private SEEK collector, e.g. C:\path\to\collector-runtime
    # focused: level and role keywords across Perth categories; broad adds all Perth ICT jobs.
    # Both add nationwide remote keyword lanes when allow_remote is true.
    # The page switches it at run time; this is the value before the first switch.
    search_mode: str = Field(default="focused", pattern="^(focused|broad)$")
    # Read only missing JDs; persist search page progress across rounds.
    # Stop on 403/429 and retain the failed page for a later scheduled/manual round.
    seek_max_pages: int = 2                    # focused: pages per keyword
    seek_max_details: int = 40
    seek_broad_max_pages: int = 15             # broad: total search pages per round
    seek_broad_max_details: int = 40
    seek_request_delay_seconds: float = 8.0    # between any two SEEK requests
    # Clear non-junior management subclasses dropped before detail requests
    seek_excluded_subclasses: str = "Management,Architects,Team Leaders,CTO/CIO"
    model_launcher: Path | None = None         # the user's own script that starts llama-server
    model_start_timeout_seconds: int = 600
    retry_failed_generation: bool = True       # a failed full generation is queued once more
    # After screening, queue eligible jobs for generation (true) or only list them as "ready" for the user
    # to pick (false). The page switches it at run time; this is the value before the first switch.
    auto_generate: bool = True

    # -- screening criteria (the user's hard exclusions) ------------------------
    exclude_title_pattern: str = (r"\b(senior|sr\.?|lead|principal|manager|head|director|architect"
                                  r"|mid[- ]?level|intermediate)\b")
    location_keyword: str = "perth"            # a listing outside this place is out unless remote is allowed
    allow_remote: bool = True
    require_full_time: bool = True
    accept_contract: bool = True               # contract/temp passes the full-time rule, with a flag

    # -- fit gate (does an eligible job suit the candidate?) ---------------------
    fit_screen_enabled: bool = True
    max_required_years: int = 2                # more *mandatory* years than this is a skip, if rule and model agree
    fit_skip_levels: list[str] = Field(default_factory=lambda: ["mid", "senior"])  # skipped when the JD says so too
    # run: no remark; flag: run with a remark; skip: out, if the title has no technical word either
    role_family_actions: dict[str, str] = Field(default_factory=lambda: {
        "software_dev": "run", "data_ai": "run", "it_support": "flag", "infrastructure": "flag",
        "security": "flag", "business_analysis": "flag", "non_technical": "skip"})
    # constraint kinds that skip a job when rule and model both find them:
    # driver_licence, wwcc, shift_work, fixed_term
    fit_skip_constraints: list[str] = Field(default_factory=list)
    profile_path: Path = PROJECT_ROOT / "experiences" / "profile.md"   # its 相关能力 lines are the known stack
