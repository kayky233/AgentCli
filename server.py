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
def get_state(run_id: str | None = None):
    """
    Return the current state snapshot.

    Priority:
    1) If run_id is provided and exists in RUN_STATE_CACHE, return that run's state.
    2) If no run_id, return the most recently updated run from RUN_STATE_CACHE (by insertion order).
    3) If cache is empty, fall back to reading agent_state.json from disk.
    """
    # 1) Explicit run_id in cache
    if run_id and run_id in RUN_STATE_CACHE:
        return JSONResponse(content=RUN_STATE_CACHE[run_id])

    # 2) Latest run from cache (Python 3.7+ dict preserves insertion order)
    if not run_id and RUN_STATE_CACHE:
        # Take the last inserted run_id
        last_run_id = next(reversed(RUN_STATE_CACHE.keys()))
        return JSONResponse(content=RUN_STATE_CACHE[last_run_id])

    # 3) Fallback: read agent_state.json
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
        run_id_disk = state.get("run_id")
        if run_id_disk:
            RUN_STATE_CACHE[run_id_disk] = state
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

    steps_done = state.get("steps_done")
    steps_total = state.get("steps_total")
    # Ensure steps fields are always present and numeric.
    try:
        steps_done = int(steps_done)
    except (TypeError, ValueError):
        steps_done = 0
    try:
        steps_total = int(steps_total)
    except (TypeError, ValueError):
        steps_total = len(state.get("stages") or [])

    resp = {
        "run_id": state.get("run_id"),
        "status": state.get("status") or "queued",
        "current_stage": state.get("current_stage"),
        "steps_done": steps_done,
        "steps_total": steps_total,
        "elapsed_ms": state.get("elapsed_ms"),
        "stages": state.get("stages") or [],
        "last_error": state.get("last_error"),
        "final_output": state.get("final_output"),
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
    SSE endpoint that streams events for a given run_id based on RUN_STATE_CACHE.

    It periodically snapshots RUN_STATE_CACHE[run_id] and emits synthesized
    events whenever logs, stages, or run status change.
    """

    def gen():
        import time as _time

        last_sent = {
            "events_len": 0,
            "stages": {},
            "status": None,
        }
        while True:
            state = RUN_STATE_CACHE.get(run_id)
            if not state:
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

    Additionally, initialize RUN_STATE_CACHE and enqueue into RUN_QUEUE so that
    /api/runs/{run_id} works and the background worker will execute the run.
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

    # Initialize run state and enqueue into worker queue so this run is visible and processed.
    stages = [
        {"name": "PLAN", "status": "pending", "message": "", "started_at": None, "ended_at": None},
        {"name": "GATHER", "status": "pending", "message": "", "started_at": None, "ended_at": None},
        {"name": "EDIT", "status": "pending", "message": "", "started_at": None, "ended_at": None},
        {"name": "APPLY", "status": "pending", "message": "", "started_at": None, "ended_at": None},
        {"name": "PREPARE", "status": "pending", "message": "", "started_at": None, "ended_at": None},
        {"name": "VERIFY_BUILD", "status": "pending", "message": "", "started_at": None, "ended_at": None},
        {"name": "VERIFY_TEST", "status": "pending", "message": "", "started_at": None, "ended_at": None},
    ]
    RUN_STATE_CACHE[run_id] = {
        "run_id": run_id,
        "task": task,
        "status": "queued",
        "current_stage": None,
        "steps_done": 0,
        "steps_total": len(stages),
        "elapsed_ms": None,
        "stages": stages,
        "last_error": None,
        "events_tail": [],
        "final_output": None,
    }
    await RUN_QUEUE.put(run_id)

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
    stages = [
        {"name": "PLAN", "status": "pending", "message": "", "started_at": None, "ended_at": None},
        {"name": "GATHER", "status": "pending", "message": "", "started_at": None, "ended_at": None},
        {"name": "EDIT", "status": "pending", "message": "", "started_at": None, "ended_at": None},
        {"name": "APPLY", "status": "pending", "message": "", "started_at": None, "ended_at": None},
        {"name": "PREPARE", "status": "pending", "message": "", "started_at": None, "ended_at": None},
        {"name": "VERIFY_BUILD", "status": "pending", "message": "", "started_at": None, "ended_at": None},
        {"name": "VERIFY_TEST", "status": "pending", "message": "", "started_at": None, "ended_at": None},
    ]
    RUN_STATE_CACHE[run_id] = {
        "run_id": run_id,
        "task": task,
        "status": "queued",
        "current_stage": None,
        "steps_done": 0,
        "steps_total": len(stages),
        "elapsed_ms": None,
        "stages": stages,
        "last_error": None,
        "events_tail": [],
        "final_output": None,
    }

    # Enqueue run_id for the worker loop.
    await RUN_QUEUE.put(run_id)

    return JSONResponse(content={"run_id": run_id})


async def worker_loop():
    """
    Background worker that consumes run_ids from RUN_QUEUE and executes the
    pipeline in a separate thread, updating RUN_STATE_CACHE and emitting logs.

    NOTE: This demo worker simulates a full pipeline by iterating through
    STAGES_ORDER and updating each stage's status.
    """
    import time as _time
    import traceback

    STAGES_ORDER = [
        "PLAN",
        "GATHER",
        "EDIT",
        "APPLY",
        "PREPARE",
        "VERIFY_BUILD",
        "VERIFY_TEST",
    ]

    while True:
        run_id = await RUN_QUEUE.get()
        state = RUN_STATE_CACHE.get(run_id)
        if not state:
            # Nothing to do; defensive check.
            RUN_QUEUE.task_done()
            continue

        start_ts = _time.time()
        state["status"] = "running"
        state["steps_done"] = 0
        stages = state.get("stages") or []
        # Ensure stages list exists and matches STAGES_ORDER
        stages_map = {st.get("name"): st for st in stages if st.get("name")}
        normalized_stages = []
        for name in STAGES_ORDER:
            st = stages_map.get(name) or {
                "name": name,
                "status": "pending",
                "message": "",
                "started_at": None,
                "ended_at": None,
            }
            normalized_stages.append(st)
        state["stages"] = normalized_stages
        state["steps_total"] = len(normalized_stages)
        RUN_STATE_CACHE[run_id] = state

        try:
            for idx, stage_name in enumerate(STAGES_ORDER):
                now = _time.time()
                state["current_stage"] = stage_name
                # mark stage running
                for st in state["stages"]:
                    if st["name"] == stage_name:
                        st["status"] = "running"
                        st["started_at"] = st.get("started_at") or now
                        break
                RUN_STATE_CACHE[run_id] = state

                # Simulate work for this stage in a worker thread.
                async def _simulate_stage():
                    def _work():
                        _time.sleep(0.5)

                    await asyncio.to_thread(_work)

                await _simulate_stage()

                # mark stage succeeded
                end_stage_ts = _time.time()
                for st in state["stages"]:
                    if st["name"] == stage_name:
                        st["status"] = "succeeded"
                        st["ended_at"] = end_stage_ts
                        break
                state["steps_done"] = idx + 1
                RUN_STATE_CACHE[run_id] = state

            # all stages done
            end_ts = _time.time()
            state["status"] = "succeeded"
            state["elapsed_ms"] = int((end_ts - start_ts) * 1000)
            # Set a simple final_output summary for the demo worker.
            state["final_output"] = {
                "answer": f"Run {run_id} completed successfully.",
                "summary": f"All {len(STAGES_ORDER)} stages finished in {state['elapsed_ms']} ms.",
            }
            # Append a final event so clients can see completion in logs.
            events = state.get("events_tail") or []
            events.append(
                {
                    "type": "final",
                    "level": "info",
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "payload": state["final_output"],
                }
            )
            state["events_tail"] = events[-50:]
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
