from pathlib import Path

from fastapi import FastAPI
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
