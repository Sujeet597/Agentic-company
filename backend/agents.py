"""
Agent definitions and orchestration for the Agentic Company.

A "Manager" agent receives a task, breaks it into sub-tasks and assigns them to
specialist agents (Frontend Dev, Backend Dev, UI Designer, Content Writer).
Each specialist runs concurrently, produces real files, and those files are
written to disk under agentic-task/<project-slug>/<agent>/...
"""

import os
import re
import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone

from anthropic import AsyncAnthropic

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-5")
MANAGER_MODEL = os.environ.get("MANAGER_MODEL", MODEL)

# Output goes under the repo's agentic-task/ folder.
OUTPUT_ROOT = Path(__file__).resolve().parent.parent / "agentic-task"

_client = None


def get_client() -> AsyncAnthropic:
    """Lazily build the Anthropic client so import never fails without a key."""
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        _client = AsyncAnthropic(api_key=api_key)
    return _client


# ---------------------------------------------------------------------------
# The team
# ---------------------------------------------------------------------------

SPECIALISTS = {
    "frontend": {
        "title": "Frontend Developer",
        "emoji": "🖥️",
        "system": (
            "You are a senior frontend developer. You write clean, modern, production-ready "
            "frontend code (HTML, CSS, JavaScript, or a framework if the task calls for it). "
            "You care about accessibility, responsive layout, and readable code. "
            "Produce complete, runnable files — never placeholders or TODOs."
        ),
    },
    "backend": {
        "title": "Backend Developer",
        "emoji": "⚙️",
        "system": (
            "You are a senior backend developer who prefers Python. You write clean, well-structured "
            "server code, APIs, data models, and business logic. Include docstrings and basic error "
            "handling. Produce complete, runnable files — never placeholders or TODOs."
        ),
    },
    "ui_designer": {
        "title": "UI Designer",
        "emoji": "🎨",
        "system": (
            "You are a product UI/UX designer. You produce a concise design system: color palette, "
            "typography, spacing, component styles, and a design rationale. Deliver a design-spec "
            "markdown file plus a reusable CSS (or tokens) file when useful. Be specific with hex "
            "values, font choices, and sizes."
        ),
    },
    "content_writer": {
        "title": "Content Writer",
        "emoji": "✍️",
        "system": (
            "You are a professional content and blog writer. You write clear, engaging copy: blog "
            "posts, landing-page copy, product descriptions, or documentation as the task requires. "
            "Deliver polished markdown. Match a friendly, credible brand voice."
        ),
    },
}

MANAGER = {
    "title": "Project Manager",
    "emoji": "🧑‍💼",
    "system": (
        "You are an engineering project manager running a small product studio. You receive a task "
        "from the client and break it into concrete assignments for your team. Your team members are: "
        "frontend (Frontend Developer), backend (Backend Developer), ui_designer (UI Designer), and "
        "content_writer (Content Writer). Assign a task to a team member ONLY if their skills are "
        "genuinely needed for this request. Write each instruction so the team member can execute it "
        "independently without asking follow-up questions."
    ),
}

# ---------------------------------------------------------------------------
# Structured-output tools (forces reliable JSON via tool_use)
# ---------------------------------------------------------------------------

PLAN_TOOL = {
    "name": "create_plan",
    "description": "Record the project plan and assignments for the team.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "A 1-2 sentence summary of how you'll approach the task.",
            },
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "agent": {
                            "type": "string",
                            "enum": list(SPECIALISTS.keys()),
                        },
                        "instruction": {
                            "type": "string",
                            "description": "Detailed, self-contained instruction for this team member.",
                        },
                        "deliverable": {
                            "type": "string",
                            "description": "Short description of what they should hand back.",
                        },
                    },
                    "required": ["agent", "instruction", "deliverable"],
                },
            },
        },
        "required": ["summary", "tasks"],
    },
}

FILES_TOOL = {
    "name": "emit_files",
    "description": "Return the finished files for your assignment.",
    "input_schema": {
        "type": "object",
        "properties": {
            "notes": {
                "type": "string",
                "description": "Short note to the manager about what you built and any decisions.",
            },
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative file path, e.g. 'index.html' or 'api/routes.py'.",
                        },
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        "required": ["notes", "files"],
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "project"


def _safe_join(root: Path, rel: str) -> Path:
    """Join and ensure the result stays inside root (block path traversal)."""
    candidate = (root / rel).resolve()
    if not str(candidate).startswith(str(root.resolve())):
        raise ValueError(f"Unsafe path: {rel}")
    return candidate


async def _call_tool(model: str, system: str, user: str, tool: dict) -> dict:
    """Call Claude forcing a single tool, return the tool input dict."""
    client = get_client()
    resp = await client.messages.create(
        model=model,
        max_tokens=8000,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": user}],
    )
    for block in resp.content:
        if block.type == "tool_use":
            return block.input
    raise RuntimeError("Model did not return the expected tool call.")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def run_manager(task: str) -> dict:
    user = (
        f"Client task:\n\n{task}\n\n"
        "Break this into assignments for your team. Only assign agents whose skills are needed."
    )
    return await _call_tool(MANAGER_MODEL, MANAGER["system"], user, PLAN_TOOL)


async def run_specialist(agent_key: str, instruction: str, original_task: str) -> dict:
    spec = SPECIALISTS[agent_key]
    user = (
        f"Overall project: {original_task}\n\n"
        f"Your assignment ({spec['title']}):\n{instruction}\n\n"
        "Return complete, ready-to-use files."
    )
    return await _call_tool(MODEL, spec["system"], user, FILES_TOOL)


def write_files(project_dir: Path, agent_key: str, files: list) -> list:
    """Write an agent's files under project_dir/<agent_key>/ and return written paths."""
    agent_dir = project_dir / agent_key
    agent_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for f in files:
        path = f.get("path", "").strip()
        if not path:
            continue
        target = _safe_join(agent_dir, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.get("content", ""), encoding="utf-8")
        written.append(str(target.relative_to(OUTPUT_ROOT)))
    return written


async def orchestrate(job: dict, on_update) -> None:
    """
    Run the full pipeline for one job. `job` is a mutable dict the API layer
    exposes to the frontend; `on_update` is called after each state change.
    """
    task = job["task"]
    project_dir = OUTPUT_ROOT / job["project_slug"]
    project_dir.mkdir(parents=True, exist_ok=True)

    # --- Phase 1: Manager plans -------------------------------------------
    job["status"] = "planning"
    job["agents"]["manager"]["status"] = "working"
    on_update()

    try:
        plan = await run_manager(task)
    except Exception as e:  # noqa: BLE001
        job["agents"]["manager"]["status"] = "error"
        job["agents"]["manager"]["notes"] = str(e)
        job["status"] = "error"
        job["error"] = f"Manager failed: {e}"
        on_update()
        return

    job["plan"] = plan
    job["agents"]["manager"]["status"] = "done"
    job["agents"]["manager"]["notes"] = plan.get("summary", "")

    # Persist the plan as a readable brief.
    brief_lines = [
        f"# Project Brief: {job['project_name']}",
        "",
        f"_Generated {datetime.now(timezone.utc).isoformat()}_",
        "",
        "## Task",
        task,
        "",
        "## Manager summary",
        plan.get("summary", ""),
        "",
        "## Assignments",
    ]
    for t in plan.get("tasks", []):
        title = SPECIALISTS.get(t["agent"], {}).get("title", t["agent"])
        brief_lines += [f"### {title}", f"- **Instruction:** {t['instruction']}",
                        f"- **Deliverable:** {t['deliverable']}", ""]
    (project_dir / "PLAN.md").write_text("\n".join(brief_lines), encoding="utf-8")

    # Mark which specialists were assigned; skip the rest.
    assigned = {t["agent"] for t in plan.get("tasks", [])}
    for key in SPECIALISTS:
        if key not in assigned:
            job["agents"][key]["status"] = "skipped"
    on_update()

    # --- Phase 2: Specialists work concurrently ---------------------------
    job["status"] = "building"
    on_update()

    async def do_one(t: dict):
        key = t["agent"]
        job["agents"][key]["status"] = "working"
        job["agents"][key]["instruction"] = t["instruction"]
        on_update()
        try:
            result = await run_specialist(key, t["instruction"], task)
            written = write_files(project_dir, key, result.get("files", []))
            job["agents"][key]["status"] = "done"
            job["agents"][key]["notes"] = result.get("notes", "")
            job["agents"][key]["files"] = written
        except Exception as e:  # noqa: BLE001
            job["agents"][key]["status"] = "error"
            job["agents"][key]["notes"] = str(e)
        on_update()

    await asyncio.gather(*(do_one(t) for t in plan.get("tasks", [])))

    job["status"] = "done"
    job["output_dir"] = str(project_dir)
    on_update()


def new_agents_state() -> dict:
    """Initial per-agent state for a fresh job."""
    state = {
        "manager": {
            "title": MANAGER["title"], "emoji": MANAGER["emoji"],
            "status": "idle", "notes": "", "files": [], "instruction": "",
        }
    }
    for key, spec in SPECIALISTS.items():
        state[key] = {
            "title": spec["title"], "emoji": spec["emoji"],
            "status": "idle", "notes": "", "files": [], "instruction": "",
        }
    return state
