"""Health, settings and the server-sent event stream."""
import json
import queue

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from .deps import get_settings, get_worker
from .schemas import HealthOut, SettingsOut

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health", response_model=HealthOut)
def health(worker=Depends(get_worker), settings=Depends(get_settings)):
    online, model = worker.model_online()
    return HealthOut(online=online, model=model if online else None, queue=worker.queue_status(),
                     autopilot_enabled=settings.autopilot_enabled)


@router.post("/model/start", status_code=202)
def start_model(request: Request, worker=Depends(get_worker), settings=Depends(get_settings)):
    """Run the user's own model launcher (RAG_MODEL_LAUNCHER) if the model is offline."""
    online, _ = worker.model_online()
    if online:
        return {"started": False, "online": True}
    if not settings.model_launcher:
        raise HTTPException(400, "未配置模型启动脚本（RAG_MODEL_LAUNCHER）")
    request.app.state.autopilot.launch(settings.model_launcher)
    return {"started": True, "online": False}


@router.get("/settings", response_model=SettingsOut)
def get_settings_view(settings=Depends(get_settings)):
    database = settings.database_url.split("://", 1)[0]
    return SettingsOut(resume_path=str(settings.resume_path), outputs_dir=str(settings.outputs_dir),
                       jobs_dir=str(settings.jobs_dir), database=database,
                       autopilot_enabled=settings.autopilot_enabled,
                       autopilot_interval_hours=settings.autopilot_interval_hours,
                       collector_dir=str(settings.collector_dir) if settings.collector_dir else None,
                       model_launcher=str(settings.model_launcher) if settings.model_launcher else None)


@router.get("/events")
def events(worker=Depends(get_worker)):
    """Server-sent events: task progress, queue changes, autopilot notices.
    A comment line every 15 seconds keeps the connection alive."""
    subscriber = worker.events.subscribe()

    def stream():
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    event = subscriber.get(timeout=15)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: {event['type']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
        finally:
            worker.events.unsubscribe(subscriber)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
