# Application workflow and release notes

## Running locally

Install `requirements.txt` and `requirements-backend.txt`, build `frontend/` with Node 22.18+ and `npm ci` / `npm run build`, then run `python -m backend.app.main`. The Windows launcher additionally expects a configured local model and `resume/base_resume.docx`. Nothing in the source-only release is a real applicant's resume or evidence.

Back up existing state and apply `python -m alembic -c backend/alembic.ini upgrade head` before upgrading. The per-version resume table is added by revision `a20261013`. Submission markers are stored by run ID, separate from the job's processing status.

## Collection settings

- `RAG_AUTOPILOT_ENABLED` defaults to false; enable scheduling explicitly.
- `RAG_COLLECTOR_DIR` is an existing private runtime directory, holding `.seek_cache`, pagination state and `seek_output`, not an external code checkout.
- `RAG_SEARCH_MODE` selects focused or broad lanes; settings and saved UI choices control generation.
- `RAG_AUTO_GENERATE=false` leaves eligible jobs ready for manual review; this is recommended when checking new search settings. The underlying application default remains true.
- Missing employer JD bodies are discarded on import. Collector metadata is excluded from location evidence.
- National remote listings require explicit employer wording supporting the candidate's location. Ambiguous interstate roles do not become eligible merely because metadata mentions remote.
- Automated collection is optional and subject to the source site's terms and access controls. On 403/429 the collector stops; examples and tests do not require live access.

## Model compatibility

The configured model ID must exist on the chosen local service. When the service rejects structured-output parameters, use `response_format: "none"`; local JSON/schema validation still applies. Remove unsupported `extra_body` fields. Permanent 4xx request rejections fail promptly with a sanitized diagnostic; transient service failures retain bounded retry behavior. A technical failure is not evidence of missing candidate skills or restricted work rights.

## Archive and submission state

Archiving a job removes it from the active board and all active resume groups without deleting artifacts or submitted history. Runs carry their own archive indicator, so a large ledger or partially loaded job page cannot resurrect them. Reingesting a manually skipped item must not silently reactivate it. Downloading a document never marks an application submitted.

## Validation limits

Synthetic tests cover API workflows, per-version submission, archive filtering, search lanes, location evidence and document structure. They do not prove the semantics of every model answer, real-world application outcomes, pixel-identical office rendering, or production cloud operation. Use private data only on a trusted local deployment.

Graceful shutdown waits for the active generation worker to finish its database writes before the application disposes its engine. An active generation can delay shutdown; forcefully terminating the process still relies on the existing restart-recovery workflow.
