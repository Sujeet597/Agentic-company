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

import json  # noqa: E402

app = FastAPI(title="Agentic Company")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# Job store, persisted to disk so projects survive server restarts.
JOBS: dict[str, dict] = {}

JOB_FILE = ".agentic-job.json"  # written inside each project folder


def _job_file(slug: str) -> Path:
    return agents.OUTPUT_ROOT / slug / JOB_FILE


def save_job(job: dict) -> None:
    """Persist the latest state for this project's folder (latest job wins)."""
    try:
        p = _job_file(job["project_slug"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(job), encoding="utf-8")
    except Exception:  # noqa: BLE001 — persistence is best-effort
        pass


def load_jobs() -> None:
    """Rebuild JOBS from disk on startup: saved jobs + any bare project folders."""
    root = agents.OUTPUT_ROOT
    if not root.exists():
        return
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        f = d / JOB_FILE
        if f.exists():
            try:
                job = json.loads(f.read_text(encoding="utf-8"))
                # Never resurrect a job as "running" after a restart.
                if job.get("status") in ("queued", "planning", "building"):
                    job["status"] = "done"
                    for a in job.get("agents", {}).values():
                        if a.get("status") == "working":
                            a["status"] = "error"
                            a["notes"] = a.get("notes") or "Interrupted by a server restart."
                JOBS[job["id"]] = job
                continue
            except Exception:  # noqa: BLE001
                pass
        # A project folder with no saved job (e.g. from before persistence) —
        # register a stub so it still appears and can be updated.
        stub_id = uuid.uuid4().hex[:12]
        JOBS[stub_id] = {
            "id": stub_id, "task": "", "project_name": d.name,
            "project_slug": d.name, "mode": "new", "parent": None,
            "status": "done", "plan": None, "error": None,
            "output_dir": str(d), "created_at": d.stat().st_mtime,
            "ended_at": None, "agents": agents.new_agents_state(),
        }


load_jobs()


class TaskRequest(BaseModel):
    task: str
    project_name: str = "untitled-project"
    parent_job_id: str | None = None   # set to update an existing project


@app.post("/api/jobs")
async def create_job(req: TaskRequest):
    if not req.task.strip():
        raise HTTPException(status_code=400, detail="Task cannot be empty.")

    job_id = uuid.uuid4().hex[:12]
    parent = JOBS.get(req.parent_job_id) if req.parent_job_id else None
    if req.parent_job_id and not parent:
        raise HTTPException(status_code=404, detail="Project to update not found.")

    if parent:
        # Update mode: reuse the same folder and continue the same project.
        slug = parent["project_slug"]
        project_name = parent["project_name"]
        mode = "update"
    else:
        slug = f"{agents.slugify(req.project_name or req.task[:40])}-{job_id[:6]}"
        project_name = req.project_name or "untitled-project"
        mode = "new"

    job = {
        "id": job_id,
        "task": req.task,
        "project_name": project_name,
        "project_slug": slug,
        "mode": mode,
        "parent": req.parent_job_id if parent else None,
        "status": "queued",
        "plan": None,
        "error": None,
        "output_dir": None,
        "created_at": time.time(),
        "ended_at": None,
        "agents": agents.new_agents_state(),
    }

    # Carry over each agent's prior session so update-mode agents keep their memory.
    if parent:
        for key, a in job["agents"].items():
            pa = parent["agents"].get(key)
            if pa and pa.get("session_id"):
                a["session_id"] = pa["session_id"]

    JOBS[job_id] = job
    save_job(job)

    def on_update():
        save_job(job)  # persist each state change so the project survives restarts

    # Fire-and-forget the orchestration.
    asyncio.create_task(_run(job, on_update))
    return {"job_id": job_id, "project_slug": slug, "mode": mode}


@app.get("/api/projects")
async def list_projects():
    """Latest job per project (newest first) — for the update selector."""
    latest: dict[str, dict] = {}
    for j in sorted(JOBS.values(), key=lambda j: j.get("created_at") or 0, reverse=True):
        latest.setdefault(j["project_slug"], j)
    return [
        {
            "job_id": j["id"],
            "project_name": j["project_name"],
            "project_slug": j["project_slug"],
            "status": j["status"],
            "created_at": j.get("created_at"),
        }
        for j in latest.values()
    ]


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
    save_job(job)

    def on_update():
        save_job(job)

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
