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

## Authentication — no API key

The app runs on the **Claude Agent SDK**, which uses your existing **Claude Code
login** (the `claude` CLI you use in the terminal). There is **no Anthropic API
key** and no separate pay-as-you-go billing — it uses the same Claude you're
already signed into. Requires the `claude` CLI and Node.js installed (they are,
if you use Claude Code).

## Setup

```powershell
cd C:\Users\V53239\agentic-company
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Make sure you're logged in to Claude Code (`claude` works in your terminal).

## Run

```powershell
# from the backend/ folder, with the venv active:
uvicorn main:app --host 127.0.0.1 --port 8001
```

Open **http://localhost:8001**, type a task, and click **Dispatch to team**.
Watch each desk go from *idle → working → done*, then check the files that
appeared under `agentic-task/`.

## How it works

- **Frontend** (`frontend/`): a static dashboard — the "studio floor". It POSTs
  the task and polls the job for live status.
- **Backend** (`backend/`):
  - `main.py` — FastAPI: serves the UI, creates jobs, runs orchestration in the
    background, exposes job status for polling. Clears any `ANTHROPIC_API_KEY`
    so auth always goes through your Claude Code login.
  - `agents.py` — the team. The manager returns a JSON plan; each specialist
    runs as a Claude Agent SDK session with file tools and writes its own
    files directly under `agentic-task/<project>/<agent>/`.
- Agents run **concurrently** (`asyncio.gather`) once the manager has planned.

## Example tasks to try

- *"Build a landing page for a travel agency with a booking form, and write a launch blog post."*
- *"Create a REST API for a todo app in FastAPI with a simple HTML frontend."*
- *"Design a dashboard UI for a fitness tracker and write onboarding copy."*

## Notes

- Uses your Claude Code default model; override with `AGENT_MODEL` in `.env`.
- Job state is in-memory — restarting the server clears run history (files on disk stay).
- Each run gets a unique folder suffix so runs never overwrite each other.
- Agents can run shell commands (e.g. the backend agent may run its own tests),
  so runs can leave `__pycache__` / `.pytest_cache` folders — that's real work, not clutter.
"# Agentic-company" 
