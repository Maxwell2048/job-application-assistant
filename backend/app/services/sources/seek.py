"""The SEEK source: runs the private collector (settings.collector_dir) and
reads the run it points to via seek_output/latest.json.

Collector code is bundled under tools/seek_collector. Cache, progress and
collected data remain in the configured private collector directory. Its JD files begin with a metadata block (title, company, location,
URL, collection time) that the pipeline can use as ordinary JD text.

Both modes add cross-category role searches and nationwide remote roles when
allowed. The bundled collector persists pagination in the private runtime
directory and retains ambiguous titles for JD screening.
"""
import json
import re
import subprocess
import sys

from datetime import datetime, timezone
from pathlib import Path

from ...core.paths import PROJECT_ROOT
from ..location_rules import employer_body

MODES = ("focused", "broad")


def run_collector(collector_dir, max_pages, max_details, options=(), timeout=3600):
    """Default runner: (returncode, stdout, stderr). Exit code 2 means partial
    errors with output still written; anything else non-zero is a failure.
    `options` are further command-line arguments (mode, filters, known ids)."""
    completed = subprocess.run([sys.executable, "-X", "utf8", str(PROJECT_ROOT / "tools" / "seek_collector" / "collect_seek.py"), "--output", str(Path(collector_dir) / "seek_output"), "--state-dir", str(collector_dir), "--max-pages", str(max_pages),
                                "--max-details", str(max_details), *options], cwd=str(collector_dir),
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return completed.returncode, completed.stdout, completed.stderr


def latest_run_dir(collector_dir):
    output = Path(collector_dir) / "seek_output"
    pointer = json.loads((output / "latest.json").read_text(encoding="utf-8"))
    return output / pointer["run_directory"]


def load_run(run_dir):
    """Jobs of one collector run as ledger items, plus the collector's report."""
    run_dir = Path(run_dir)
    rows = json.loads((run_dir / "jobs.json").read_text(encoding="utf-8"))
    report = json.loads((run_dir / "run_report.json").read_text(encoding="utf-8"))
    items = []
    for row in rows:
        jd_text = ""
        if row.get("jd_file"):
            try:
                jd_text = (run_dir / row["jd_file"]).read_text(encoding="utf-8")
            except OSError:
                jd_text = ""
        if not employer_body(jd_text):
            continue
        items.append({"source": "seek", "external_id": str(row["job_id"]), "title": row.get("title", ""),
                      "company": row.get("company", ""), "url": row.get("url", ""),
                      "location": row.get("location", ""), "work_type": row.get("work_type", ""),
                      "listed_at": row.get("listed_at", ""), "jd_text": jd_text, "expired": bool(row.get("expired")),
                      "collector_status": row.get("detail_status", "")})
    report["discarded_no_jd"] = len(rows) - len(items)
    return items, report


def mode_limits(settings, mode):
    """(max_pages, max_details) defaults of a mode."""
    if mode == "broad":
        return settings.seek_broad_max_pages, settings.seek_broad_max_details
    return settings.seek_max_pages, settings.seek_max_details


BLOCK_FILENAME = "seek_block.json"        # the bundled collector's own cool-down record, kept in its state dir
QUEUE_FILENAME = "pending_details.txt"   # job ids still owed a full JD, independent of what the ledger stores


def _state_file(settings, name):
    directory = getattr(settings, "collector_dir", None)
    return Path(directory) / name if directory else None


def block_state(settings):
    """What refused the last round and until when; {} when nothing is recorded."""
    path = _state_file(settings, BLOCK_FILENAME)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        return {}                                    # an unreadable or half-written note never blocks a round
    return data if isinstance(data, dict) else {}


def cool_down_open(settings, now=None):
    """True while the collector is still standing down from a refusal."""
    until = block_state(settings).get("until")
    try:
        end = datetime.fromisoformat(str(until)) if until else None
    except ValueError:
        return False                                 # a malformed timestamp is treated as expired, not eternal
    if end is None:
        return False
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) < end


def queued_ids(settings, limit=400):
    """Ids the collector promised but has not read yet. They live beside its cache because a listing
    without a JD never becomes a ledger row, so no database query can find them."""
    path = _state_file(settings, QUEUE_FILENAME)
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path else []
    except OSError:
        return []                                    # nothing promised yet is not an error
    ids, seen = [], set()
    for line in lines:
        ident = line.strip()
        if ident.isdigit() and ident not in seen:
            seen.add(ident)                          # order is the promise; duplicates are noise
            ids.append(ident)
    return ids[:limit]


def format_cooldown(value):
    """A cool-down deadline in local wall-clock form for the round's notes; '' when absent."""
    try:
        moment = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return ""
    if moment is None:
        return ""
    if moment.tzinfo is not None:
        moment = moment.astimezone()                 # the page shows the user their own clock, not UTC
    return moment.strftime("%H:%M")


def collector_options(settings, mode, known_ids_file=None, pending_ids_file=None, backlog_only=False):
    options = ["--expanded-search", "--include-remote" if settings.allow_remote else "--no-include-remote", "--mode", mode, "--delay", str(settings.seek_request_delay_seconds),
               "--worktypes", "242,244" if settings.accept_contract else "242",
               "--exclude-subclass", settings.seek_excluded_subclasses,
               "--exclude-title", settings.exclude_title_pattern,
               # Refusal handling: switch transports rather than lose the round, and stay under the edge's threshold.
               "--transport", getattr(settings, "seek_transport_mode", "auto"),
               "--max-list-per-hour", str(getattr(settings, "seek_max_list_per_hour", 12)),
               "--max-details-per-hour", str(getattr(settings, "seek_max_details_per_hour", 40)),
               "--jitter-factor", str(getattr(settings, "seek_jitter_factor", 1.8))]
    chrome = getattr(settings, "chrome_path", None)
    if chrome:
        options += ["--browser-path", str(chrome)]
    if backlog_only:                                  # a post-refusal round drains the queue with no list requests at all
        options += ["--backlog-only"]
    if known_ids_file is not None:
        options += ["--known-ids", str(known_ids_file)]
    if pending_ids_file is not None:
        options += ["--pending-ids", str(pending_ids_file)]
    return options


def write_ids(settings, name, ids):
    """Job ids for the collector, one per line, in a file it can take. Order is kept."""
    path = Path(settings.jobs_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(ids) + "\n", encoding="utf-8")
    return path


def write_known_ids(settings, known_ids):
    """The ids the collector need not re-read."""
    return write_ids(settings, "_seek_known_ids.txt", sorted(known_ids))


def collect(settings, max_pages=None, max_details=None, runner=run_collector, mode="focused", known_ids=(),
            pending_ids=(), backlog_only=False):
    """-> (items, info). Raises RuntimeError when the collector is not configured
    or failed outright. known_ids: SEEK ids whose JD the ledger already holds.
    pending_ids: ids known only by title, most wanted first; the collector
    reads their details before it spends any request on search pages. It keeps a backlog file of its
    own too, so listings that never reached the ledger are still owed a read. backlog_only runs a
    drain round: detail requests only, no search page touched."""
    if mode not in MODES:
        raise ValueError(f"未知的搜索模式：{mode}")
    if not settings.collector_dir or not (Path(settings.collector_dir) / "collect_seek.py").is_file():
        raise RuntimeError("未配置 SEEK 采集器（RAG_COLLECTOR_DIR），或目录里没有 collect_seek.py")
    default_pages, default_details = mode_limits(settings, mode)
    known_file = write_known_ids(settings, known_ids) if known_ids else None
    pending_file = write_ids(settings, "_seek_pending_ids.txt", list(pending_ids)) if pending_ids else None
    code, stdout, stderr = runner(settings.collector_dir, default_pages if max_pages is None else max_pages, default_details if max_details is None else max_details,
                                  collector_options(settings, mode, known_file, pending_file, backlog_only))
    if code not in (0, 2):
        tail = "\n".join((stderr or stdout).strip().splitlines()[-3:])
        if re.search(r"unrecognized arguments", stderr or ""):
            tail += "\n采集器版本过旧，不支持搜索模式参数"   # e.g. a runtime directory still holding an older collector copy
        raise RuntimeError(f"采集器退出码 {code}：{tail}")
    run_dir = latest_run_dir(settings.collector_dir)
    items, report = load_run(run_dir)
    info = {"run_dir": str(run_dir), "exit_code": code, "mode": mode, "unique_jobs": report.get("unique_jobs"),
            "full_descriptions": report.get("full_descriptions"), "discarded_no_jd": report.get("discarded_no_jd", 0), "card_filtered": report.get("card_filtered", 0),
            "known_skipped": report.get("known_skipped", 0), "rate_limited": bool(report.get("rate_limited")),
            "backlog_only": report.get("backlog_only", False), "detail_deferred": report.get("detail_deferred", 0),
            "pending_requested": report.get("pending_requested", 0), "pending_read": report.get("pending_read", 0),
            # Why the round stopped, in the collector's own words: which refusal, how long to wait, what is still owed.
            "blocked_by": report.get("blocked_by"), "block_code": report.get("block_code"),
            "refusal_label": report.get("refusal_label"), "cooldown_until": report.get("cooldown_until"),
            "deferred_for_cool_down": bool(report.get("deferred_for_cool_down")),
            "quota_exhausted": report.get("quota_exhausted"), "transport": report.get("transport"),
            "transport_switched": bool(report.get("transport_switched")),
            "browser_available": bool(report.get("browser_available")),
            "network_requests": report.get("network_requests", 0), "http_attempts": report.get("http_attempts", 0),
            "browser_attempts": report.get("browser_attempts", 0), "queue_remaining": report.get("queue_remaining"),
            "queue_local_requested": report.get("queue_local_requested", 0),
            "errors": report.get("errors", []),
            "search_truncated": report.get("search_truncated"), "search_complete": report.get("search_complete", False),
            "search_pages": report.get("search_pages", []), "resume_cursor": report.get("resume_cursor")}
    return items, info
