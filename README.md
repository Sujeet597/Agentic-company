# Agentic Studio

A small "AI company" you run locally. Type a task; a **Project Manager** agent
breaks it down and dispatches real work to four specialist agents:

| Agent | Role |
|-------|------|
| 🧑‍💼 Project Manager | Reads the task, plans it, assigns each specialist |
| 🖥️ Frontend Developer | HTML / CSS / JS (or a framework) |
| ⚙️ Backend Developer | Python APIs, models, logic |
| 🎨 UI Designer | Design system, tokens, spec |
| ✍️ Content Writer | Blog posts, copy, docs |

Every deliverable is written to disk under `agentic-task/<your-project>/<agent>/`.
The manager only assigns the specialists a task actually needs.

## Setup

```powershell
cd C:\Users\V53239\agentic-company
copy .env.example .env       # then edit .env and paste your Anthropic API key

cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Get a key at https://console.anthropic.com — it goes in `.env` as `ANTHROPIC_API_KEY`.

## Run

```powershell
# from the backend/ folder, with the venv active:
uvicorn main:app --reload
```

Open **http://localhost:8000**, type a task, and click **Dispatch to team**.
Watch each desk go from *idle → working → done*, then check the files that
appeared under `agentic-task/`.

## How it works

- **Frontend** (`frontend/`): a static dashboard — the "studio floor". It POSTs
  the task and polls the job for live status.
- **Backend** (`backend/`):
  - `main.py` — FastAPI: serves the UI, creates jobs, runs orchestration in the
    background, exposes job status for polling.
  - `agents.py` — the team. The manager uses a forced tool call to return a
    structured plan; each specialist uses a forced tool call to return files,
    which are written safely under `agentic-task/`.
- Agents run **concurrently** (`asyncio.gather`) once the manager has planned.

## Example tasks to try

- *"Build a landing page for a travel agency with a booking form, and write a launch blog post."*
- *"Create a REST API for a todo app in FastAPI with a simple HTML frontend."*
- *"Design a dashboard UI for a fitness tracker and write onboarding copy."*

## Notes

- Model defaults to `claude-sonnet-5`; override with `AGENT_MODEL` / `MANAGER_MODEL` in `.env`.
- Job state is in-memory — restarting the server clears run history (files on disk stay).
- Each run gets a unique folder suffix so runs never overwrite each other.
"# Agentic-company" 
