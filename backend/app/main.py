"""FastAPI application: JSON API under /api, the built frontend at /.

    python -m backend.app.main            # http://127.0.0.1:8000, docs at /docs

Binds to 127.0.0.1 only: run directories hold personal material and the
model API key stays in config.local.json.
"""
import argparse
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .api import autopilot as autopilot_api
from .api import jobs, runs, system, tasks
from .core.config import Settings
from .db.session import create_schema, make_engine, make_session_factory
from .services.autopilot import Autopilot
from .services.ledger import relink_jd_files
from .services.worker import Worker

PLACEHOLDER = """<!doctype html><meta charset="utf-8"><title>Job Application Assistant API</title>
<body style="font-family:system-ui;margin:3rem;line-height:1.6">
<h1>Job Application Assistant</h1>
<p>后端已运行。前端尚未构建：在 <code>frontend/</code> 运行 <code>npm run build</code> 后刷新本页。</p>
<p>接口文档：<a href="/docs">/docs</a> · 健康检查：<a href="/api/health">/api/health</a></p></body>"""


def create_app(settings: Settings | None = None, runner=None, autopilot_kwargs=None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.database_url)
    if settings.auto_create_schema:
        create_schema(engine)
    session_factory = make_session_factory(engine)
    worker = Worker(settings, session_factory, runner=runner)
    autopilot = Autopilot(settings, session_factory, worker, **(autopilot_kwargs or {}))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        relink_jd_files(session_factory, settings.jobs_dir)   # the project folder may have moved
        worker.recover()            # jobs a previous run left queued or half-done go back in the queue
        worker.start()
        autopilot.start()
        yield
        autopilot.stop()
        worker.stop()
        engine.dispose()  # release the SQLite file so the directory can be removed (tests, restarts)

    app = FastAPI(title="Job Application Assistant", version="0.2", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.worker = worker
    app.state.autopilot = autopilot
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"],
                       allow_headers=["*"])
    for router in (system.router, jobs.router, tasks.router, runs.router, autopilot_api.router):
        app.include_router(router)

    dist = Path(settings.frontend_dist)
    if (dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")

        @app.middleware("http")
        async def revalidate_index(request, call_next):
            # index.html names the current bundle; without this a browser may keep showing the
            # previous build after an update. The hashed assets under /assets/ stay cacheable.
            response = await call_next(request)
            if response.headers.get("content-type", "").startswith("text/html"):
                response.headers["Cache-Control"] = "no-cache"
            return response
    else:
        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        def placeholder():
            return PLACEHOLDER
    return app


def main(argv=None):
    import uvicorn
    parser = argparse.ArgumentParser(description="求职 RAG 后端")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    settings = Settings()
    host, port = args.host or settings.host, args.port or settings.port
    print(f"打开 http://{host}:{port}  （接口文档 /docs）", file=sys.stderr)
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
