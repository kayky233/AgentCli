from pathlib import Path
import json
from datetime import datetime
from typing import Dict, Any

import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, StreamingResponse

app = FastAPI()

# In-memory store for simple run status querying by run_id (derived from agent_state.json).
RUN_STATE_CACHE: Dict[str, Dict[str, Any]] = {}

# Async queue for run execution in the FastAPI worker.
RUN_QUEUE: "asyncio.Queue[str]" = asyncio.Queue()


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


@app.get("/api/debug/queue")
async def debug_queue():
    """
    Return the current approximate queue length for the in-process worker.
    """
    return JSONResponse(content={"queue_length": RUN_QUEUE.qsize()})


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
    Legacy endpoint: append an incoming task to pending_tasks.json.

    NOTE: For new implementations prefer POST /api/runs, which enqueues a run
    directly into the in-process worker queue and returns run_id immediately.
    """
    data = await request.json()
    task = (data.get("task") or "").strip()
    if not task:
        return JSONResponse(
            content={"error": "task is required"},
            status_code=400,
        )

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


@app.post("/api/runs")
async def create_run(request: Request):
    """
    Create a new run and enqueue it into the background worker queue.

    Request JSON:
      { "task": "..." }

    Response JSON:
      { "run_id": "..." }
    """
    data = await request.json()
    task = (data.get("task") or "").strip()
    if not task:
        return JSONResponse(
            content={"error": "task is required"},
            status_code=400,
        )

    run_id = datetime.utcnow().strftime("%Y%m%d%H%M%S")

    # Initialize a minimal state snapshot for this run as queued.
    RUN_STATE_CACHE[run_id] = {
        "run_id": run_id,
        "task": task,
        "status": "queued",
        "current_stage": None,
        "steps_done": 0,
        "steps_total": 0,
        "elapsed_ms": None,
        "stages": [],
        "last_error": None,
        "events_tail": [],
    }

    # Enqueue run_id for the worker loop.
    await RUN_QUEUE.put(run_id)

    return JSONResponse(content={"run_id": run_id})


async def worker_loop():
    """
    Background worker that consumes run_ids from RUN_QUEUE and executes the
    pipeline in a separate thread, updating RUN_STATE_CACHE and emitting logs.

    NOTE: The actual orchestration/pipeline execution remains in the CLI app.
    This worker is a placeholder to demonstrate queue consumption and state
    transitions without blocking the FastAPI event loop.
    """
    import time as _time
    import traceback

    while True:
        run_id = await RUN_QUEUE.get()
        state = RUN_STATE_CACHE.get(run_id)
        if not state:
            # Nothing to do; defensive check.
            RUN_QUEUE.task_done()
            continue

        start_ts = _time.time()
        state["status"] = "running"
        state["current_stage"] = "PLAN"
        state["stages"] = [
            {"name": "PLAN", "status": "running", "message": "", "started_at": start_ts, "ended_at": None},
        ]
        state["steps_total"] = 1
        state["steps_done"] = 0
        RUN_STATE_CACHE[run_id] = state

        try:
            # Simulate a blocking pipeline call in a worker thread.
            async def _simulate_pipeline():
                def _work():
                    _time.sleep(2.0)

                await asyncio.to_thread(_work)

            await _simulate_pipeline()

            # Mark stage completed
            end_ts = _time.time()
            state["stages"][0]["status"] = "succeeded"
            state["stages"][0]["ended_at"] = end_ts
            state["steps_done"] = 1
            state["status"] = "succeeded"
            state["elapsed_ms"] = int((end_ts - start_ts) * 1000)
            state["current_stage"] = "PLAN"
            RUN_STATE_CACHE[run_id] = state
        except Exception:
            end_ts = _time.time()
            tb = traceback.format_exc()
            state["status"] = "failed"
            state["elapsed_ms"] = int((end_ts - start_ts) * 1000)
            state["last_error"] = {"traceback": tb}
            RUN_STATE_CACHE[run_id] = state
        finally:
            RUN_QUEUE.task_done()


@app.on_event("startup")
async def startup_event():
    # Start background worker loop.
    asyncio.create_task(worker_loop())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
