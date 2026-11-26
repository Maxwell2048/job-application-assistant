"""Scale benchmark for the list endpoints the web page loads at start-up.

Builds a throw-away ledger with synthetic jobs and run directories, then times
the board (/api/jobs, all pages), the run list and the resume list, and counts
the SQL statements each one issues. Nothing personal is read and no model is
needed.

    python -X utf8 tools/perf/bench_list_api.py --jobs 1000 --runs 300
    python -X utf8 tools/perf/bench_list_api.py --jobs 5000 --runs 1500 --json

"first" is a backend that has never cached a summary (the first start after an
upgrade), "restart" a new backend process reusing the stored summaries, and
"steady" a repeated request in the same process.
"""
import argparse
import json
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

from backend.app.core.config import Settings  # noqa: E402
from backend.app.db.models import Job, RagRun, ResumeApplication, Screening  # noqa: E402
from backend.app.main import create_app  # noqa: E402

ENDPOINTS = {"board": "/api/jobs", "runs": "/api/runs", "resumes": "/api/runs?resumes_only=true&limit=5000"}


def build(root, jobs, runs):
    outputs = root / "outputs"
    settings = Settings(_env_file=None, project_root=root, jobs_dir=root / "jobs", outputs_dir=outputs,
                        resume_path=root / "r.docx", config_path=root / "c.json", frontend_dist=root / "nodist",
                        database_url=f"sqlite:///{(root / 'state.sqlite').as_posix()}")
    app = create_app(settings)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with app.state.session_factory() as session:
        for n in range(1, jobs + 1):
            job = Job(source="synthetic", external_id=str(n), title=f"Role {n}", company=f"Company {n % 97}",
                      status="done" if n <= runs else ("screened_out" if n % 5 else "ready"),
                      last_seen=start + timedelta(minutes=n))
            session.add(job)
            session.flush()
            session.add(Screening(job_id=job.id, decision="run" if n <= runs else "skip",
                                  rule_result={"fit": {"level": "junior"}}))
            if n <= runs:
                run_id = f"20260101-{n:06d}-abcdef"
                write_run(outputs / run_id, run_id, n)
                session.add(RagRun(job_id=job.id, run_id=run_id, run_status="complete"))
                if n % 3 == 0:
                    session.add(ResumeApplication(run_id=run_id, submitted_at=start))
        session.commit()
    app.state.engine.dispose()
    # Read every new file once, as a real ledger's runs were written long before
    # the page loads: otherwise on-access virus scanning of just-created files
    # dominates the first measurement.
    for path in outputs.rglob("*"):
        if path.is_file():
            path.read_bytes()
    return settings


def write_run(run, run_id, n):
    run.mkdir(parents=True, exist_ok=True)
    requirements = [{"requirement_id": f"R{i:03d}", "category": "technical_skill",
                     "importance": "required" if i % 3 else "preferred", "text": f"Skill {i}"} for i in range(1, 25)]
    matches = [{"requirement_id": r["requirement_id"], "processing_status": "ok",
                "verdict": ("direct", "related", "insufficient")[i % 3], "reason": "x" * 400}
               for i, r in enumerate(requirements)]
    (run / "run_meta.json").write_text(json.dumps({"run_id": run_id, "run_status": "complete",
                                                   "started_at": "2026-01-01T00:00:00",
                                                   "input": {"jd_path": f"jobs/synthetic-{n}.txt"}}), encoding="utf-8")
    (run / "requirements.json").write_text(json.dumps({"requirements": requirements}), encoding="utf-8")
    (run / "matches.json").write_text(json.dumps({"matches": matches}), encoding="utf-8")
    (run / "quality.json").write_text(json.dumps({"checks": [], "score": 1}), encoding="utf-8")
    for name in ("report.md", "cv_suggestions.md", "resume_tailored.md"):
        (run / name).write_text("# synthetic\n", encoding="utf-8")
    (run / "cv_suggestions.json").write_text("{}", encoding="utf-8")
    (run / f"Sample_Resume_Company_Role_{n:06d}.docx").write_bytes(b"PK")


def fetch(client, name):
    if name != "board":
        return client.get(ENDPOINTS[name]).json()
    out, offset = [], 0
    while True:
        batch = client.get(f"/api/jobs?limit=1000&offset={offset}").json()
        out += batch
        if len(batch) < 1000:
            return out
        offset += 1000


def measure(settings, repeat):
    results = {}
    for phase in ("first", "restart"):
        app = create_app(settings)          # a new process: empty in-memory index
        statements = []
        event.listen(app.state.engine, "before_cursor_execute", lambda *a: statements.append(1))
        client = TestClient(app)
        for name in ENDPOINTS:
            statements.clear()
            started = time.perf_counter()
            rows = fetch(client, name)
            results.setdefault(name, {})[phase] = {"seconds": round(time.perf_counter() - started, 3),
                                                   "queries": len(statements), "rows": len(rows)}
        if phase == "restart":
            for name in ENDPOINTS:
                times = []
                for _ in range(repeat):
                    started = time.perf_counter()
                    fetch(client, name)
                    times.append(time.perf_counter() - started)
                results[name]["steady"] = {"seconds": round(statistics.median(times), 3)}
        app.state.engine.dispose()
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=int, default=1000)
    parser.add_argument("--runs", type=int, default=300, help="jobs with a generated run directory")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        settings = build(Path(tmp), args.jobs, min(args.runs, args.jobs))
        results = measure(settings, args.repeat)
    if args.json:
        print(json.dumps({"jobs": args.jobs, "runs": args.runs, "results": results}, indent=2))
        return 0
    print(f"{args.jobs} jobs, {min(args.runs, args.jobs)} run directories")
    for name, phases in results.items():
        first, restart, steady = phases["first"], phases["restart"], phases["steady"]
        print(f"  {name:8s} first {first['seconds']:6.3f}s ({first['queries']} queries)   "
              f"restart {restart['seconds']:6.3f}s ({restart['queries']} queries)   steady {steady['seconds']:6.3f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
