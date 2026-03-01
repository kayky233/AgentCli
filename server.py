from pathlib import Path
import json
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse

app = FastAPI()


@app.get("/state")
def get_state():
    """
    Return the current agent_state.json contents, or a minimal default if missing.
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
        return FileResponse(state_path, media_type="application/json")
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
