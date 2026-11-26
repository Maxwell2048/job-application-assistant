"""Cached run summaries, so listing pages do not re-read every run directory.

run_summary() opens several JSON files and globs each run directory. The board
and the resume list ask for every run on every refresh, so the cost grew with
the number of runs (about 2 s for 215 runs). This index keeps each summary
together with a signature of the directory's files and recomputes it only when
the signature changes.

* Signature: one os.scandir of the run directory, hashing every regular file's
  name, size and modification time (nanoseconds). Any write, new export or
  deletion changes it. run_summary() reads only files inside the directory, so
  an unchanged signature means an unchanged summary.
* Two layers: an in-process dictionary, and the run_summary_cache table so a
  freshly started backend does not rebuild every summary on its first request.
* run_summary() stays the single source of truth. A summary from the index is
  exactly what run_summary() returned for the same files.
* SUMMARY_VERSION is part of every cached row; bump it whenever run_summary()
  changes what it reports, and every stored summary is rebuilt on first use.
* Persisting is best effort: if the table cannot be written (a locked SQLite
  file, a read-only database), the request still gets correct summaries.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..db.models import RunSummaryCache
from .runs import run_summary

log = logging.getLogger(__name__)

SUMMARY_VERSION = 1
_QUERY_CHUNK = 500          # keeps IN (...) lists under SQLite's bound-parameter limit


@dataclass(frozen=True)
class RunListing:
    """What one scandir of a run directory tells us."""
    signature: str
    names: frozenset[str]


def scan_run_dir(run_dir) -> RunListing | None:
    """The directory's file signature and file names, or None when it is not a directory."""
    digest = hashlib.sha1()
    names = []
    try:
        with os.scandir(run_dir) as entries:
            for entry in entries:
                try:
                    if not entry.is_file():
                        continue
                    stat = entry.stat()
                except OSError:
                    continue            # removed between listing and stat
                names.append((entry.name, stat.st_size, stat.st_mtime_ns))
    except (FileNotFoundError, NotADirectoryError):
        return None
    for name, size, mtime in sorted(names):
        digest.update(f"{name}\0{size}\0{mtime}\n".encode("utf-8", "surrogatepass"))
    return RunListing(digest.hexdigest(), frozenset(name for name, _, _ in names))


class RunIndex:
    def __init__(self, session_factory=None, version: int = SUMMARY_VERSION):
        self.session_factory = session_factory
        self.version = version
        self._memory: dict[str, tuple[str, dict]] = {}      # run_id -> (signature, summary)
        self._lock = threading.Lock()

    def summaries(self, run_dirs, listings: dict[str, RunListing | None] | None = None) -> dict[str, dict]:
        """{run_id: summary} for the run directories that exist. Each summary is
        a fresh shallow copy, so callers may add keys to it."""
        listings = dict(listings or {})
        wanted: dict[str, tuple[Path, str]] = {}
        for run_dir in run_dirs:
            run_dir = Path(run_dir)
            listing = listings[run_dir.name] if run_dir.name in listings else scan_run_dir(run_dir)
            if listing is not None:
                wanted[run_dir.name] = (run_dir, listing.signature)

        found: dict[str, dict] = {}
        with self._lock:
            for run_id, (_, signature) in wanted.items():
                cached = self._memory.get(run_id)
                if cached is not None and cached[0] == signature:
                    found[run_id] = cached[1]

        missing = {run_id: value for run_id, value in wanted.items() if run_id not in found}
        if missing:
            stored = self._load(missing)
            fresh = {}
            for run_id, (run_dir, signature) in missing.items():
                summary = stored.get(run_id)
                if summary is None:
                    summary = run_summary(run_dir)
                    fresh[run_id] = (signature, summary)
                found[run_id] = summary
            with self._lock:
                for run_id, (_, signature) in missing.items():
                    self._memory[run_id] = (signature, found[run_id])
            if fresh:
                self._store(fresh)
        return {run_id: dict(summary) for run_id, summary in found.items()}

    def summary(self, run_dir) -> dict | None:
        return self.summaries([run_dir]).get(Path(run_dir).name)

    def forget(self, run_id: str):
        """Drop a run from the in-process layer (its stored row is re-validated on next use)."""
        with self._lock:
            self._memory.pop(run_id, None)

    # -- persistence ------------------------------------------------------------
    def _load(self, missing):
        """Stored summaries whose signature and version still match."""
        if self.session_factory is None:
            return {}
        out = {}
        ids = list(missing)
        try:
            with self.session_factory() as session:
                for start in range(0, len(ids), _QUERY_CHUNK):
                    chunk = ids[start:start + _QUERY_CHUNK]
                    for row in session.scalars(select(RunSummaryCache).where(RunSummaryCache.run_id.in_(chunk))):
                        if row.version == self.version and row.signature == missing[row.run_id][1]:
                            out[row.run_id] = row.summary
        except SQLAlchemyError as error:
            log.warning("run summary cache unavailable: %s", error)
            return {}
        return out

    def _store(self, fresh):
        if self.session_factory is None:
            return
        now = datetime.now(timezone.utc)
        try:
            with self.session_factory() as session:
                ids = list(fresh)
                rows = {}
                for start in range(0, len(ids), _QUERY_CHUNK):
                    chunk = ids[start:start + _QUERY_CHUNK]
                    rows.update((row.run_id, row) for row in session.scalars(
                        select(RunSummaryCache).where(RunSummaryCache.run_id.in_(chunk))))
                for run_id, (signature, summary) in fresh.items():
                    row = rows.get(run_id)
                    if row is None:
                        row = RunSummaryCache(run_id=run_id)
                        session.add(row)
                    row.signature, row.version, row.summary, row.updated_at = signature, self.version, summary, now
                session.commit()
        except SQLAlchemyError as error:
            log.warning("run summary cache not saved: %s", error)
