# Changelog

Notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
entries link to the commit or pull request that made the change. The project has no numbered releases yet,
so changes are grouped by date.

## 2026-11-26

### Changed

- The board, run list and resume list no longer slow down as jobs and runs accumulate. With 5,000 jobs and 1,500 runs on the measuring machine, the board fell from 5.7 s and 11,506 SQL statements to 0.4–1.1 s and 32 statements. The run and resume lists fell from about 3–4 s to under 0.7 s. Responses are unchanged. See [docs/performance.md](docs/performance.md).
  - Screenings, runs and application status are batch-loaded for the whole page instead of one query per job or run.
  - Run summaries come from a run index (`backend/app/services/run_index.py`). It is keyed by a signature of each run directory's files, held in memory and in the new `run_summary_cache` table, and rebuilt only for runs whose files changed.
  - A new index, `ix_jobs_last_seen`, serves the board's sort order. Alembic revision `a20261126` adds it together with the cache table.
  - Start-up JD relinking lists the jobs directory once, instead of checking every file separately.
  - The page coalesces refreshes triggered by bursts of server events (`frontend/src/coalesce.ts`).

### Added

- `tools/perf/bench_list_api.py`, a scale benchmark on synthetic data that reports timings and SQL statement counts. New regression tests require the board and resume list to issue the same number of statements for 3 jobs as for 43.

### Documentation

- Both READMEs were rewritten around the questions a first-time reader asks: what this is, how to deploy it, how to use it. The technical explanation moved into an eight-stage table, screenshots and a documentation index. The source layout now lists all 24 root modules, including `quality_report.py`, `application_answer.py` and `resume_review.py`, which were previously undocumented.
- Corrected the interpreter recommendation: the workflow is tested on Python 3.12 and CI also runs 3.13, so 3.11 is no longer described as recommended. Both language versions now describe the same clone-based setup.
- Added measured resource use (virtual environment, E5 weights, frontend build), a five-question FAQ, and the table of `RAG_` backend settings.
- Repository metadata: the description now states the concrete capabilities, and the topic tags follow the actual stack. `flask` was never used; the backend is FastAPI.

## 2026-11-25

### Added

- LangGraph engine: the backend generation stages run as a `StateGraph` (`backend/app/services/generation_graph.py`). The graph routes the entry point (save, match or a redo step), stops at the coverage gate, and runs interview preparation on request. Each run is checkpointed in memory, and its visited nodes and timings go to `graph_trace.json`. Default `RAG_GENERATION_ENGINE=langgraph`; `classic` keeps the plain Python flow. Tests run both engines and require identical results.
- LangChain chat adapter `langchain_openai` (`langchain_client.py`): the same request through `ChatOpenAI`, with the existing validation, retries and error types. It refuses to start while LangSmith tracing is on.
- LangChain retriever (`"retriever": "langchain"`, `langchain_retrieval.py`): the same E5 chunks in an `InMemoryVectorStore` with identical ranking, plus a `BaseRetriever` that returns one Document per section with citation metadata.
- `docs/langchain-langgraph.md` describes the three switches, the graph and how equivalence was checked.
- An `LLM Frameworks: LangChain · LangGraph` skills row (`build_resume.frameworks_line`). It is added for AI, software and data roles, or when the JD mentions LLMs, agents or RAG, and only when the profile records the candidate's confirmation, which it cites. It is grouped with the declared AI tooling rows. Tailoring prompt version 10.

## 2026-11-24

### Added

- Internship search lanes: focused mode also searches `internship` and a quoted `intern` (so "Internal Tools" titles do not match), and those two lanes include SEEK's Casual/Vacation work type, where summer and vacation programmes are listed.
- Fit screening asks whether a posting is only for current students (currently enrolled, penultimate or final year, returning to study). When the model and the wording rule agree, the job is skipped because a graduate cannot take it; when only one side sees it, the job runs with a hint. A posting that also accepts recent graduates is not treated as student-only. Switch with `RAG_FIT_SKIP_STUDENT_ONLY`. Fit prompt version 3.

### Changed

- Intern and internship wording counts as junior-level evidence in the collector and the fit screen, and an intern title is never dropped as senior before its JD is read.

## 2026-11-22

### Fixed

- Detect a local model whose engine reports a different name for the same model on the same port. An endpoint in `local_model_endpoints` can now list several accepted IDs in `models`, tried in order after `model`. Before this, switching the launcher to an engine that serves only the canonical name (no alias) made the app report the model as offline although it was loaded.
- The offline message now says what each port actually reports and which names the configuration accepts, instead of only listing the ports.
- The public release now includes the modules the resume, quality-report and application-answer stages import (`job_family`, `resume_skills`, `resume_review`, `quality_report`, `application_answer`) and their synthetic tests. A new test fails whenever released code imports a root module that the release leaves out.

## 2026-11-21

### Added

- A quality report for every run (`quality_report.py`, `quality.json`): flags internal checking language, unsupported admin or service-desk claims, tools named only in the JD and leftover repair wording. The run page shows a badge and a panel listing each finding.
- Redo single generation steps (tailoring, closing, assembly, export) from the run page instead of regenerating the whole run (`POST /api/runs/{id}/redo`).
- Application question answers drafted from the same evidence and kept within the form's word limit (`application_answer.py`, `POST /api/runs/{id}/answers`).
- A per-run log of every model call (`llm_calls.jsonl`) for diagnosing slow or failed stages.
- Health reports which loaded modules changed since the backend started (`restart_needed`), and the page asks for a restart.
- A coverage gate: an automatically queued job whose conditions mostly lack evidence (default share 0.7) stops at the match report with status `low_coverage`.
- Resume headlines follow the target job family, skills are ordered by what the JD asks for, and projects are reviewed and ordered by evidence before assembly (`job_family.py`, `resume_skills.py`, `resume_review.py`).
- An optional model preflight that validates one synthetic extraction before a batch drains.

### Changed

- Tailoring prompt version 9: plain, HR-readable framing without adding tools, numbers, employers, roles or outcomes; a failing extra citation is dropped instead of forcing a rewrite; bullets are written for the six best-evidenced projects plus every job held.
- Screening skips any job with mandatory years of experience by default (`max_required_years=0`), still only when the rule and the model agree.

## 2026-10-21

### Added

- Classify a SEEK refusal instead of merely counting it: an anti-bot challenge (`cf-mitigated: challenge`, body "Just a moment", no `Retry-After`) is recorded apart from a plain rate limit that does carry one, and each round reports which happened together with its real status code. Measured here: an eight-second burst of search pages was refused for ~12-25 minutes afterwards.
- A cool-down window beside the cache (`seek_block.json`). While it is open a round spends no request at all unless another transport can answer, so a scheduled round cannot re-enter the lockout blind.
- A local backlog queue (`pending_details.txt`): listings that never produced a JD have no ledger row and could not be found by any database query, so they are now promised to a file that survives the round; collector option `--backlog-only` drains it without touching one search page (~1.2 MB each).
- Browser fallback transport (collector option `--transport auto|http|chrome`). A refused scripted request is retried in place through a locally installed headless Chromium browser, and the rest of the round follows it — measured at 4.5-5.6 s per warm navigation against ~0.7 s scripted, but it passes checks the scripted client cannot while locked out.
- Rolling hourly request budgets persisted next to the cache (default 12 search pages and 40 detail pages) plus deterministic jitter between requests, so several rounds inside one hour cannot collectively retrigger the refusal instead of only reacting to it.

### Changed

- Round notes name what actually stopped the round — challenge, rate limit or spent hourly budget — together with how long to wait, whether the browser took over, and how many unread jobs remain queued. The blanket "HTTP 429/403" wording is gone from both backend notes and the report page, which now separates jobs already held from jobs still unread.

## 2026-10-16

### Added

- Publish the FastAPI/React application, Alembic ledger migrations, task progress, manual JD submission and optional collection workflow.
- Group resume versions by date and persist each version's submission state independently of downloads.
- Include the SEEK collector source, persisted search-page progress, focused IT role lanes and synthetic regression tests.
- Extend CI to backend, collector and frontend regression suites and production frontend builds.

### Fixed

- Keep archived jobs out of active boards and resume counts, including ledgers spanning multiple API pages.
- Discard title-only results on import and require employer evidence for remote work from the target location.
- Surface local-model request incompatibilities without repeating permanent failures; preserve local validation when wire-format constraints are disabled.
- Retain relevant baseline profile evidence and assemble English resume headings with a consistent single-column Word layout.

## 2026-09-27

### Added

- Details before search pages: SEEK answers 429 after three or four requests, so each round now spends its
  requests on the ledger's title-only jobs first (junior and graduate titles first, jobs skipped by hand never),
  and only what is left on search pages (collector option `--pending-ids`).
- JDs pasted by hand: a title-only job's page has a "补充 JD" box with a link to the SEEK page; the pasted text is
  recorded like a collected JD (listing facts in front) and screened at once (`POST /api/jobs/{id}/jd`). The
  submit page can "先筛选，不直接生成" a pasted JD (`screen_first`).

- Search-only rounds: with "只搜索和筛选" on the autopilot page (`POST /api/autopilot/generate`, `tick --no-generate`)
  a round collects, records and screens as before, but eligible jobs go to a new "ready" column instead of the
  generation queue. "全部生成" on the board (`POST /api/jobs/generate-ready`) queues them best fit first. The
  choice is saved with the search mode.
- JD paths in the ledger are re-linked on start-up when the project folder has moved, so a copy from D: to E:
  keeps working.

### Fixed

- Skipping a job, or generating one, from its page now refreshes the board at once (and every other open page):
  skipped jobs used to stay in "新发现 / 等待" until a full reload.
- Jobs known only by their search-result title are folded into a collapsible "只有标题、待读取 JD" list instead of
  filling the "新发现 / 等待" column.

## 2026-09-23

### Added

- Two search modes for the SEEK collector, switched on the autopilot page (`POST /api/autopilot/mode`,
  `python -m backend.app.autopilot mode`) and kept in `state/autopilot_mode.json`: focused (graduate, junior,
  entry level, trainee) and broad (every ICT job in Perth). Excluded subclassifications and senior titles are
  dropped from the search results before any detail request, jobs whose JD the ledger holds are not re-read, and
  a round that SEEK rate-limits stops and leaves the rest to the next round.
- Contract/temp listings pass the work-type rule with a flag (`RAG_ACCEPT_CONTRACT`).
- Long unattended batches: the queue is ordered (pasted JDs first, then screened jobs by fit rank), pauses while
  the model is offline and starts it with the launcher, retries a failed full generation once, and puts back on
  start-up the jobs a previous run left queued or half-done. The sidebar shows why the queue is paused.
- Fit screen after the work-rights screen (`screen.fit_gate`): the local model extracts level, mandatory years,
  role family, required stack, degree and practical constraints with verified quotes; rules give a second opinion
  on years, level, constraints and the title. A job is skipped only when both agree; support roles, stack gaps,
  a non-IT degree and constraints become hints on the job. Settings: `RAG_FIT_SCREEN_ENABLED`,
  `RAG_MAX_REQUIRED_YEARS`, `RAG_FIT_SKIP_LEVELS`, `RAG_ROLE_FAMILY_ACTIONS`, `RAG_FIT_SKIP_CONSTRAINTS`,
  `RAG_PROFILE_PATH`. The result is
  stored under `rule_result["fit"]` of the screening, so existing ledgers need no migration.
- Run summaries carry `coverage`: evidence verdicts for the job's conditions and the count of hard gaps (degree,
  certificates or licences, work rights, three or more years). The board orders finished jobs by it; cards and the
  job page show it together with the fit screen's level, role family, hints and quotes.

### Changed

- The default title exclusion also covers "mid-level" and "intermediate".

## 2026-09-20

### Fixed

- A requirement for "permanent" work rights is no longer read as plain full work rights: the screening prompt
  says so and a rule second opinion catches it when the model does not.
- `jd_parser.locate_quote` returned an index error for a whitespace-only quote; it now reports "not found".
- `match_job` left `run_status` at its earlier value when an exception ended the run; every failure path now
  records `failed`.
- `tailor_cv`, `build_resume` and `interview_prep` exit with 2 when their input is unusable (missing or
  unreadable files, a corrupt `.docx`), so the backend no longer treats it as the accepted "partial" exit 1.
- `tailor_cv --letter-only` and `build_resume` check that citations saved in `cv_suggestions.json` still match
  the experience library, and ask for a fresh `tailor_cv` run when the library changed underneath them.
- `interview_prep` matched its forbidden "Honest" phrases as substrings ("expert" inside "expertise"); they are
  whole-word matches now, and a batch that runs out of budget is reported once instead of once per question.
- Docker: the build context had no `.dockerignore`, so `COPY . .` would have put `experiences/`, `resume/`,
  `config.local.json` and `.env` into the api image and the host's `node_modules` over the one `npm ci` installed
  in the frontend image. Both contexts now have one; `config.docker.json` is git-ignored and `.env.example` lists
  the `POSTGRES_*` variables `compose.yaml` asks for.
- Docker: the api container mounts the host's Hugging Face cache read-only. The pipeline runs the retriever
  offline, so without the cache the match stage could not load the embedding model. The image's command now
  `exec`s python so `docker stop` shuts the app down cleanly.
- `build_resume` also reports a corrupt `.docx` as an input error instead of a traceback.
- Flask is no longer listed in `requirements.txt` and `requirements-test.txt`; nothing has imported it since the
  Flask page was removed.
- Frontend: a slow response for a previous job can no longer overwrite the job page being shown, toast timers
  are cleared, and an autopilot run that died before writing a report renders instead of breaking the page.

## 2026-09-19

### Added

- FastAPI backend (`backend/`) over the generation engine: a ledger of jobs, screenings, runs and tasks
  (SQLAlchemy, Alembic, SQLite by default, PostgreSQL via `RAG_DATABASE_URL`), a single-slot worker that persists
  task state and streams progress over server-sent events, and a JSON API documented at `/docs`.
- Autopilot: every N hours (or on request) start the model if needed, collect job descriptions from `jobs/inbox/`
  and an optional private collector, dedupe and record them, screen them (listing rules, a model verdict on the
  work-rights requirement with a verbatim quote, program checks on the quote, a rule second opinion) and queue the
  eligible ones. Disagreements run flagged rather than being dropped.
- React + TypeScript frontend (`frontend/`, Vite): board, job page with screening quote and live progress,
  manual submission, autopilot status and history, run history, settings. Served by the backend from
  `frontend/dist`; `start.bat` builds it once when Node is present.
- `compose.yaml` and `Dockerfile.api`: PostgreSQL + API + nginx-served frontend, model server on the host.
- CI builds the frontend and runs the backend tests alongside the engine tests.

### Removed

- The Flask page (`webapp.py`, `webui/`) and its launcher path; the frontend replaces it.

## 2026-09-17

### Added

- Hiring company and job title are read from the JD by a new `job_identity` step. Both values must be wording
  of the JD, and an employer the JD does not name gives `null` rather than a guess (ef1dc92).
- The web page can abort a running job. Stage processes are stopped with their children, and an aborted
  pasted-JD job removes its unfinished run directory and saved JD file (22f2a77).
- Interview preparation is off by default and can be generated later for any finished run (22f2a77).
- Location gate: the pipeline stops before generation when a role requires on-site work outside the
  candidate's stated region (62266c0).

### Changed

- `tailor_cv.py --letter-only` regenerates only a missing summary or cover letter and keeps the bullets of the
  latest suggestions (#3).
- Word exports are named `<Name>_Resume_<Company>_<Job-Title>_<MMDD-HHMM>.docx`, English only and company
  first, so files for different applications cannot be confused (22f2a77, ef1dc92).
- The summary and the cover letter are generated by separate requests, and resume assembly generates a
  missing summary on its own (ef1dc92).

### Fixed

- An assembled resume could contain a Chinese project heading copied from the experience file's title. Each
  project now gets an English resume title, every resume and cover-letter line is checked for Chinese text,
  and Word export refuses a document that still contains any (#6).
- The cover letter rejected product names from the JD such as "Microsoft 365" as invented numbers (#7).
- Resume assembly failed when the generated summary was longer than 500 characters: the summary is now
  limited to 450 characters and program-filled lines are no longer sent back for shortening (#2).
- The cover letter always failed with an experience library written in Chinese: the model offered translated
  sentences as quotes. Translated quotes now get a specific hint, loose matching ignores Chinese punctuation,
  and an invalid citation is dropped when the paragraph keeps another valid one (#3).
- The assembled resume had no SUMMARY when a cover-letter citation was rejected (ef1dc92).
- Word export left a large blank area on page one: the "Other projects" block was one tall table row that
  Word moved to the next page as a whole. Projects are now laid out two per row (22f2a77).
- The web page treated a partial match as a failure and showed an unrelated stderr tail; it now continues
  and reports the recorded errors (2f06cc0).
- Evidence quotes that differed from the source only in case, spacing, punctuation or Markdown marks failed
  the whole judgment; they are now mapped back to the verbatim source text (2f06cc0).
- Qualifier words that occur several times in a JD ("Basic", "Strong") forced a manual review; qualifiers are
  now located inside their own requirement first (2f06cc0).
- Interview preparation rejected names used by the JD, such as "Microsoft 365", as invented numbers (2f06cc0).
- Resume assembly could never pass its retries when a program-filled line was validated a second time, and
  the number check treated `1,288` and `1288` as different numbers (a65e345).
- `start.bat` could not find the base resume because of a control character in the path (01b5965).
- Ignore rules for private directories also hid `examples/experiences/` (c5937f0).

## 2026-09-17 — first public release

- Source-only public release with fictional examples, a public evaluation set, and release tooling that
  copies an allowlist of files and scans them for private patterns (3aea7c2).
