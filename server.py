from pathlib import Path
import json
from datetime import datetime
from typing import Dict, Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, StreamingResponse

app = FastAPI()

# In-memory store for simple run status querying by run_id (derived from agent_state.json).
RUN_STATE_CACHE: Dict[str, Dict[str, Any]] = {}


@app.get("/state")
def get_state():
    """
    Return the current agent_state.json contents, or a minimal default if missing.
    Also refresh an in-memory cache keyed by run_id for /api/runs/{run_id}.
    """
    state_path = Path("agent_state.json")
    if not state_path.exists():
        return JSONResponse(
            content={
                "iteration": None,
                "current_stage": None,
                "task": None,
                "policy": {},
                "options": {},
                "applied_files": [],
                "last_build_result": None,
                "last_test_result": None,
                "events_tail": [],
            }
        )
    try:
        raw = state_path.read_text(encoding="utf-8")
        state = json.loads(raw)
        run_id = state.get("run_id")
        if run_id:
            RUN_STATE_CACHE[run_id] = state
        return JSONResponse(content=state)
    except Exception:
        return JSONResponse(
            content={"error": "failed to read state file"},
            status_code=500,
        )


@app.get("/", response_class=HTMLResponse)
def index():
    """
    Serve the minimal dashboard HTML.
    """
    index_path = Path(__file__).parent / "index.html"
    if not index_path.exists():
        return HTMLResponse("<html><body><h1>agent_state dashboard missing index.html</h1></body></html>")
    return FileResponse(index_path)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    """
    Return a snapshot of a single run by run_id.

    Shape:
    {
      "run_id": ...,
      "status": "queued"/"running"/"succeeded"/"failed",
      "current_stage": ...,
      "steps_done": int,
      "steps_total": int,
      "elapsed_ms": int|None,
      "stages": [ {name,status,message,started_at,ended_at}, ... ],
      "last_error": {...} | None
    }
    """
    # Prefer cached state (populated by /state polling), fallback to reading agent_state.json.
    state = RUN_STATE_CACHE.get(run_id)
    state_path = Path("agent_state.json")
    if not state and state_path.exists():
        try:
            raw = state_path.read_text(encoding="utf-8")
            maybe = json.loads(raw)
            if maybe.get("run_id") == run_id:
                state = maybe
                RUN_STATE_CACHE[run_id] = state
        except Exception:
            state = None

    if not state:
        return JSONResponse(
            content={"error": f"run {run_id} not found"},
            status_code=404,
        )

    resp = {
        "run_id": state.get("run_id"),
        "status": state.get("status") or "queued",
        "current_stage": state.get("current_stage"),
        "steps_done": state.get("steps_done", 0),
        "steps_total": state.get("steps_total", 0),
        "elapsed_ms": state.get("elapsed_ms"),
        "stages": state.get("stages") or [],
        "last_error": state.get("last_error"),
    }
    return JSONResponse(content=resp)


@app.get("/api/runs/{run_id}/events")
def stream_run_events(run_id: str):
    """
    Very simple SSE endpoint that streams events for a given run_id.

    It reads agent_state.json periodically and emits synthesized events
    whenever the underlying state changes (log snapshot & stage updates).
    """
    state_path = Path("agent_state.json")

    def gen():
        import time as _time

        last_sent = {
            "events_len": 0,
            "stages": {},
            "status": None,
        }
        while True:
            if not state_path.exists():
                _time.sleep(1.0)
                continue
            try:
                raw = state_path.read_text(encoding="utf-8")
                state = json.loads(raw)
            except Exception:
                _time.sleep(1.0)
                continue

            # Only stream for matching run_id, if provided in state.
            if state.get("run_id") not in (None, "", run_id):
                _time.sleep(1.0)
                continue

            # Logs: treat events_tail as log events.
            events = state.get("events_tail") or []
            ev_len = len(events)
            if ev_len > last_sent["events_len"]:
                new_events = events[last_sent["events_len"] :]
                last_sent["events_len"] = ev_len
                for ev in new_events:
                    payload = json.dumps(ev, ensure_ascii=False)
                    yield f"event: log\ndata: {payload}\n\n"

            # Stage state changes.
            for st in state.get("stages") or []:
                name = st.get("name")
                status = st.get("status")
                if not name:
                    continue
                key = f"{name}"
                prev_status = last_sent["stages"].get(key)
                if prev_status != status:
                    last_sent["stages"][key] = status
                    ev_type = "stage_start" if status == "running" else "stage_end"
                    payload = json.dumps(st, ensure_ascii=False)
                    yield f"event: {ev_type}\ndata: {payload}\n\n"

            # Run end
            status = state.get("status")
            if status in ("succeeded", "failed") and last_sent["status"] != status:
                last_sent["status"] = status
                payload = json.dumps(
                    {
                        "run_id": state.get("run_id"),
                        "status": status,
                        "elapsed_ms": state.get("elapsed_ms"),
                    },
                    ensure_ascii=False,
                )
                yield f"event: run_end\ndata: {payload}\n\n"

            _time.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/task")
async def add_task(request: Request):
    """
    Append an incoming task to pending_tasks.json (one JSON object per line).
    Expected payload: {"task": "..."}.
    Also returns a provisional run_id so the frontend can focus a run immediately.
    """
    data = await request.json()
    task = (data.get("task") or "").strip()
    if not task:
        return JSONResponse(
            content={"error": "task is required"},
            status_code=400,
        )

    # Provisional run_id; the real orchestrator run_id will usually match this timestamp-based id.
    run_id = datetime.utcnow().strftime("%Y%m%d%H%M%S")

    entry = {
        "task": task,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "run_id": run_id,
    }
    pending_path = Path("pending_tasks.json")
    try:
        with pending_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        return JSONResponse(
            content={"error": f"failed to append task: {e}"},
            status_code=500,
        )
    return {"status": "received", "task": entry, "run_id": run_id}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
