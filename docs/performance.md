# List endpoint performance

The web page loads three lists when it opens and again after server events: the board (`GET /api/jobs`, every page), the run list (`GET /api/runs`) and the resume list (`GET /api/runs?resumes_only=true`). Before 2026-11-26, the cost of each request grew with the number of jobs and generated runs, and the page became slow to open once a few hundred runs had built up.

## What made them slow

| Cause | Effect |
| --- | --- |
| Screenings and runs were loaded lazily, job by job | 2 + 2N SQL statements for N jobs (2,302 for 1,000 jobs; 11,506 for 5,000) |
| Every request rebuilt every run summary from disk | each summary globs the run directory several times and parses `matches.json` twice, `requirements.json`, `quality.json` and `run_meta.json` |
| One query per run for the application status | on the board, one `ResumeApplication` lookup per run |
| Every page event triggered a full reload | a burst of events during screening or generation started overlapping reloads of all three lists |
| Start-up checked each JD file with its own `stat` | one system call per job at every start |

## What changed

1. **Batch loading.** `list_jobs` loads screenings and runs with `selectinload` (one query per relationship for the whole page) and looks up application status for every run on the page with chunked `IN` queries. The number of statements no longer depends on the number of jobs.
2. **Run index** (`backend/app/services/run_index.py`). Each run summary is cached together with a signature of its directory: one `os.scandir` that hashes every file's name, size and modification time in nanoseconds. A changed, added or deleted file changes the signature, and only that summary is rebuilt. `run_summary()` stays the single source of truth; the cache stores what it returned.
   - Two layers: in-process memory, and the `run_summary_cache` table, so a restarted backend does not rebuild every summary on its first request.
   - `SUMMARY_VERSION` is stored with each row. Bump it whenever `run_summary()` changes what it reports; stale rows are then rebuilt on first use.
   - The table holds derived data only and is safe to delete. If it cannot be read or written, for example because the SQLite file is locked, requests still return correct summaries and a warning is logged.
   - Running tasks are still summarised directly, so their progress is never served from the cache.
3. **Index for the board's sort order.** `ix_jobs_last_seen` serves `ORDER BY last_seen DESC` (Alembic revision `a20261126`, which also creates the cache table).
4. **Start-up JD relinking** lists the jobs directory once and compares file names, instead of checking every stored path separately.
5. **Coalesced page refreshes** (`frontend/src/coalesce.ts`). While a reload is running, further events only mark it dirty, and exactly one more reload follows. A burst of events costs at most two reloads, and an older response cannot overwrite a newer one.

The API responses are unchanged.

## Measurements

`tools/perf/bench_list_api.py` builds a throw-away ledger with synthetic jobs and run directories, then times each list and counts its SQL statements:

```powershell
.\.venv\Scripts\python.exe -X utf8 tools\perf\bench_list_api.py --jobs 1000 --runs 300
.\.venv\Scripts\python.exe -X utf8 tools\perf\bench_list_api.py --jobs 5000 --runs 1500 --json
```

The script reports three timings:

- **first**: a backend that has never cached a summary, as on the first start after upgrading.
- **restart**: a new backend process that reuses the stored summaries.
- **steady**: a repeated request in the same process.

The script reads every synthetic file once before measuring, so that on-access virus scanning of newly created files does not distort the first timing.

The results below were measured on one Windows 11 machine with Python 3.14 and SQLite. Before is the previous commit and after is this change, both run with the same script. Timings vary by machine and disk; the statement counts do not.

| Ledger size | List | Before: time, statements | After, restart: time, statements | After, steady |
| --- | --- | --- | --- | --- |
| 1,000 jobs, 300 runs | board | 1.58 s, 2,302 | 0.16 s, 8 | 0.09 s |
| | runs | 0.80 s, 3 | 0.10 s, 3 | 0.12 s |
| | resumes | 0.84 s, 3 | 0.06 s, 3 | 0.11 s |
| 5,000 jobs, 1,500 runs | board | 5.69 s, 11,506 | 1.10 s, 32 | 0.43 s |
| | runs | 2.73 s, 5 | 0.60 s, 5 | 0.25 s |
| | resumes | 4.05 s, 5 | 0.70 s, 5 | 0.65 s |

The first request after upgrading builds every summary once. With the new code that took 0.66 s for 300 runs and 3.4 s for 1,500 runs; later requests are served from the cache.

## Guarding against regressions

`backend/tests/test_run_index.py` covers both parts of the change.

- **Statement counts.** The tests count SQL statements for the board and the resume list with 3 jobs and again with 43, and require the two counts to be equal. On the previous code the board count grew from 10 to 130.
- **The run index.**
  - A cached summary is exactly what `run_summary()` returns.
  - A changed, added or deleted file rebuilds the summary.
  - A restarted backend reuses stored rows, and a new `SUMMARY_VERSION` rebuilds them.
  - An unwritable cache still returns correct results.
  - Callers receive copies they can change.
  - More than 500 runs are read and stored in chunks.

`frontend/tests/coalesce.test.ts` checks that overlapping refreshes never run in parallel and that exactly one follow-up runs.

## Not done yet

- The board still downloads every job, archived ones included, on each refresh; at 5,000 jobs that is most of the remaining 0.4 to 1.1 s. Server-side filtering of archived jobs, or loading them only on the archive page, would remove it, but that requires changes to the page.
- Responses carry no `ETag`, so an unchanged list is still serialised and sent again.
