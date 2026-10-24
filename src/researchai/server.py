"""FastAPI app: start research sessions, stream their events (SSE), apply user controls.

Run: .venv/bin/uvicorn researchai.server:app --reload
"""

import asyncio
import re
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from researchai.config import get_settings
from researchai.memory import Store, make_suggester
from researchai.session import ArchivedSession, ResearchSession
from researchai.summary import build_summary, summary_markdown

WEB_DIR = Path(__file__).parent / "web"

app = FastAPI(title="ResearchAI")


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    """Make browsers revalidate the UI files on every load, so a stale app.js can't be
    paired with a newer page (ETags still make unchanged files cheap 304s)."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response
sessions: dict[str, ResearchSession] = {}  # live sessions started since the server came up
store = Store(get_settings().db_path)  # finished sessions, kept across restarts
_tasks: set[asyncio.Task] = set()  # keep references so background runs aren't GC'd


_runs: dict[str, asyncio.Task] = {}  # session id -> its research run, so delete can stop it


def _background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def _get(session_id: str) -> ResearchSession | ArchivedSession:
    session = sessions.get(session_id)
    if session is not None:
        return session
    record = store.load(session_id)
    if record is None:
        raise HTTPException(404, "Unknown session")
    return ArchivedSession(record)


class StartRequest(BaseModel):
    topic: str


class ControlRequest(BaseModel):
    action: Literal["pause", "resume", "skip", "exclude", "answer"]
    question_number: int | None = None
    source_id: str | None = None
    answers: dict[str, list[str] | str] | None = None  # direction choices by question header


@app.get("/")
async def index() -> HTMLResponse:
    html = (WEB_DIR / "index.html").read_text()
    for asset in ("app.js", "style.css"):  # version asset URLs by file change time
        version = int((WEB_DIR / asset).stat().st_mtime)
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={version}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.post("/api/sessions")
async def start_session(req: StartRequest) -> dict:
    topic = req.topic.strip()
    if not topic:
        raise HTTPException(400, "Topic is empty")
    settings = get_settings()
    missing = settings.missing_keys()
    if missing:
        raise HTTPException(500, f"Missing keys in .env: {', '.join(missing)}")
    session = ResearchSession(topic, settings, store=store, suggester=make_suggester(settings))
    session.answer_provider = session.await_submitted_answer
    sessions[session.id] = session
    _runs[session.id] = _background(session.run())
    return {"id": session.id}


@app.get("/api/sessions")
async def list_sessions() -> list[dict]:
    live = {
        s.id: {"id": s.id, "topic": s.topic, "status": s.status, "paused": s.controls.paused,
               "created": s.created.isoformat(timespec="seconds")}
        for s in sessions.values()
    }
    saved = [s for s in store.list() if s["id"] not in live]
    return sorted([*live.values(), *saved], key=lambda s: s["created"], reverse=True)


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    return _get(session_id).snapshot()


def _summary(session_id: str) -> dict:
    session = _get(session_id)
    return build_summary(session.snapshot(), [e.model_dump(mode="json") for e in session.events])


@app.get("/api/sessions/{session_id}/summary")
async def get_summary(session_id: str) -> dict:
    return _summary(session_id)


@app.get("/api/sessions/{session_id}/summary.md")
async def download_summary(session_id: str) -> PlainTextResponse:
    summary = _summary(session_id)
    slug = re.sub(r"[^a-z0-9]+", "-", summary["overview"]["topic"].lower()).strip("-")[:60]
    return PlainTextResponse(
        summary_markdown(summary), media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{slug or "research"}-summary.md"'},
    )


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict:
    """Delete a session for good: stop it if it's still running, and remove it from storage."""
    live = sessions.pop(session_id, None)
    run = _runs.pop(session_id, None)
    if run and not run.done():
        run.cancel()
    stored = store.delete(session_id)
    if live is None and not stored:
        raise HTTPException(404, "Unknown session")
    return {"ok": True}


@app.get("/api/sessions/{session_id}/events")
async def stream_events(session_id: str) -> EventSourceResponse:
    session = _get(session_id)

    async def events():
        backlog, queue = session.subscribe()
        try:
            for event in backlog:
                yield {"event": "log", "data": event.model_dump_json()}
            while True:
                event = await queue.get()
                yield {"event": "log", "data": event.model_dump_json()}
        finally:
            session.unsubscribe(queue)

    return EventSourceResponse(events(), ping=15)


@app.post("/api/sessions/{session_id}/control")
async def control(session_id: str, req: ControlRequest) -> dict:
    session = _get(session_id)
    if isinstance(session, ArchivedSession):
        raise HTTPException(400, "This is a saved session from an earlier run; it is read-only.")
    error = None
    match req.action:
        case "pause":
            session.pause()
        case "resume":
            session.resume()
        case "skip":
            if req.question_number is None:
                raise HTTPException(400, "question_number is required")
            error = session.skip(req.question_number)
        case "exclude":
            if not req.source_id:
                raise HTTPException(400, "source_id is required")
            error = session.exclude(req.source_id)
            if not error and session.status == "done":
                _background(session.revise_if_needed())
        case "answer":
            error = session.submit_answer(req.answers or {})
    if error:
        raise HTTPException(400, error)
    return {"ok": True}


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
