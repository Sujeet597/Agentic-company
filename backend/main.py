"""
FastAPI app: serves the dashboard UI and orchestrates the agent team.

Run:
    cd backend
    pip install -r requirements.txt
    uvicorn main:app --reload
Then open http://localhost:8000
"""

import os
import time
import uuid
import asyncio
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Load .env for optional overrides (e.g. AGENT_MODEL) from the project root.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Force the Claude Agent SDK to authenticate via your local Claude Code login
# instead of a pay-as-you-go API key. Remove any key that .env/env may have set.
os.environ.pop("ANTHROPIC_API_KEY", None)

import agents  # noqa: E402  (import after env is prepared)

app = FastAPI(title="Agentic Company")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# In-memory job store. Fine for a local single-user tool.
JOBS: dict[str, dict] = {}


class TaskRequest(BaseModel):
    task: str
    project_name: str = "untitled-project"


@app.post("/api/jobs")
async def create_job(req: TaskRequest):
    if not req.task.strip():
        raise HTTPException(status_code=400, detail="Task cannot be empty.")

    job_id = uuid.uuid4().hex[:12]
    slug = agents.slugify(req.project_name or req.task[:40])
    # Keep slugs unique so runs don't overwrite each other.
    slug = f"{slug}-{job_id[:6]}"

    job = {
        "id": job_id,
        "task": req.task,
        "project_name": req.project_name or "untitled-project",
        "project_slug": slug,
        "status": "queued",
        "plan": None,
        "error": None,
        "output_dir": None,
        "created_at": time.time(),
        "ended_at": None,
        "agents": agents.new_agents_state(),
    }
    JOBS[job_id] = job

    def on_update():
        # State lives in the shared dict; polling reads it. Nothing to push.
        pass

    # Fire-and-forget the orchestration.
    asyncio.create_task(_run(job, on_update))
    return {"job_id": job_id, "project_slug": slug}


async def _run(job: dict, on_update):
    try:
        await agents.orchestrate(job, on_update)
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(e)


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return JSONResponse({**job, "server_now": time.time()})


@app.post("/api/jobs/{job_id}/agents/{agent_key}/continue")
async def continue_agent(job_id: str, agent_key: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    a = job["agents"].get(agent_key)
    if not a or agent_key == "manager":
        raise HTTPException(status_code=400, detail="Not a resumable agent.")
    if a["status"] == "working":
        raise HTTPException(status_code=409, detail="Agent is already working.")

    # Flip status synchronously so the next poll sees 'working' (no race).
    a["status"] = "working"
    a["notes"] = ""
    job["status"] = "building"

    def on_update():
        pass

    asyncio.create_task(_resume(job, agent_key, on_update))
    return {"ok": True}


async def _resume(job: dict, agent_key: str, on_update):
    try:
        await agents.resume_agent(job, agent_key, on_update)
    except Exception as e:  # noqa: BLE001
        job["agents"][agent_key]["status"] = "error"
        job["agents"][agent_key]["notes"] = str(e)
        agents.recompute_status(job)


@app.get("/api/health")
async def health():
    # Auth comes from your local Claude Code login (Claude Agent SDK), not an API key.
    return {"ok": True, "auth": "claude-code"}


# --- Static frontend ------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
