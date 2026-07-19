"""
Agent definitions and orchestration for the Agentic Company.

Runs on the **Claude Agent SDK**, which uses your local Claude Code login — no
separate API key or billing. A "Manager" agent breaks the task into assignments;
each specialist agent (Frontend Dev, Backend Dev, UI Designer, Content Writer)
runs with file tools and writes its deliverables directly to disk under
agentic-task/<project-slug>/<agent>/.
"""

import os
import re
import json
import time
import asyncio
from pathlib import Path
from datetime import datetime, timezone

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    ResultMessage,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Leave unset to use whatever model your Claude Code login defaults to.
MODEL = os.environ.get("AGENT_MODEL") or None

OUTPUT_ROOT = Path(__file__).resolve().parent.parent / "agentic-task"

FILE_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

# ---------------------------------------------------------------------------
# The team
# ---------------------------------------------------------------------------

SPECIALISTS = {
    "frontend": {
        "title": "Frontend Developer",
        "emoji": "🖥️",
        "system": (
            "You are a senior frontend developer. You write clean, modern, production-ready "
            "frontend code (HTML, CSS, JavaScript, or a framework if the task calls for it), "
            "caring about accessibility, responsive layout, and readable code."
        ),
    },
    "backend": {
        "title": "Backend Developer",
        "emoji": "⚙️",
        "system": (
            "You are a senior backend developer who prefers Python. You write clean, well-structured "
            "server code, APIs, data models, and business logic with docstrings and basic error handling."
        ),
    },
    "ui_designer": {
        "title": "UI Designer",
        "emoji": "🎨",
        "system": (
            "You are a product UI/UX designer. You produce a concise design system: color palette, "
            "typography, spacing, component styles, and a design rationale. Deliver a design-spec "
            "markdown file plus a reusable CSS/tokens file when useful, with specific hex values and sizes."
        ),
    },
    "content_writer": {
        "title": "Content Writer",
        "emoji": "✍️",
        "system": (
            "You are a professional content and blog writer. You write clear, engaging copy — blog "
            "posts, landing-page copy, product descriptions, or documentation — delivered as polished markdown."
        ),
    },
}

MANAGER = {
    "title": "Project Manager",
    "emoji": "🧑‍💼",
    "system": (
        "You are an engineering project manager running a small product studio. You receive a task "
        "and break it into concrete assignments for your team: frontend (Frontend Developer), backend "
        "(Backend Developer), ui_designer (UI Designer), content_writer (Content Writer). Assign a task "
        "to a team member ONLY if their skills are genuinely needed."
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "project"


# Substrings that mark a retryable, transient failure (network / streaming / load).
_TRANSIENT = (
    "stalled", "mid-stream", "overloaded", "rate limit", "rate_limit",
    "timeout", "timed out", "econnreset", "socket", "network",
    "api error", "500", "502", "503", "504", "529",
)


def _is_transient(err: Exception) -> bool:
    msg = str(err).lower()
    return any(t in msg for t in _TRANSIENT)


async def _run_once(system: str, prompt: str, cwd: Path | None,
                    allowed_tools: list | None, max_turns: int,
                    resume_session: str | None, session_out: list | None) -> str:
    opts = ClaudeAgentOptions(
        system_prompt=system,
        cwd=str(cwd) if cwd else None,
        allowed_tools=allowed_tools or [],
        permission_mode="bypassPermissions",  # autonomous — never prompt for tool use
        max_turns=max_turns,
        setting_sources=None,                  # don't inherit project/user CLAUDE.md or settings
        model=MODEL,
        resume=resume_session,                 # continue a prior session when given
    )
    text_parts: list[str] = []
    async for msg in query(prompt=prompt, options=opts):
        sid = getattr(msg, "session_id", None)
        if sid and session_out is not None:
            if session_out:
                session_out[0] = sid
            else:
                session_out.append(sid)
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
        elif isinstance(msg, ResultMessage):
            if getattr(msg, "is_error", False):
                detail = getattr(msg, "result", None) or "agent reported an error"
                raise RuntimeError(str(detail))
    text = "".join(text_parts).strip()
    # The CLI sometimes injects a stall notice as text without flagging is_error.
    low = text.lower()
    if "stalled mid-stream" in low or "response above may be incomplete" in low:
        raise RuntimeError("Response stalled mid-stream.")
    return text


async def _run(system: str, prompt: str, cwd: Path | None = None,
               allowed_tools: list | None = None, max_turns: int = 30,
               retries: int = 3, resume_session: str | None = None,
               session_out: list | None = None) -> str:
    """Run an agent turn-loop, retrying transient stream/load failures with backoff.

    session_out (if given) receives the SDK session id at index 0 so the caller
    can later resume the same conversation.
    """
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            return await _run_once(system, prompt, cwd, allowed_tools, max_turns,
                                   resume_session, session_out)
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries - 1 and _is_transient(e):
                await asyncio.sleep(3 * (attempt + 1))  # 3s, 6s
                continue
            raise
    raise last_err  # pragma: no cover


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a text blob (tolerates stray prose/fences)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else None
    if raw is None:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        raw = brace.group(0) if brace else None
    if raw is None:
        raise RuntimeError("Manager did not return a JSON plan.")
    return json.loads(raw)


def scan_files(agent_dir: Path) -> list:
    """Files the agent wrote, relative to the output root."""
    if not agent_dir.exists():
        return []
    return sorted(
        str(p.relative_to(OUTPUT_ROOT))
        for p in agent_dir.rglob("*")
        if p.is_file()
    )


# ---------------------------------------------------------------------------
# The agents
# ---------------------------------------------------------------------------

async def run_manager(task: str) -> dict:
    system = (
        MANAGER["system"]
        + " Respond with ONLY a JSON object — no prose, no markdown fences. Shape: "
        '{"summary": string, "tasks": [{"agent": "frontend"|"backend"|"ui_designer"|"content_writer", '
        '"instruction": string, "deliverable": string}]}. Each instruction must be self-contained.'
    )
    prompt = (
        f"Client task:\n\n{task}\n\n"
        "Break this into assignments for your team. Only assign agents whose skills are needed. "
        "Return the JSON plan now."
    )
    text = await _run(system, prompt, allowed_tools=[], max_turns=2)
    return _extract_json(text)


async def run_specialist(agent_key: str, instruction: str, original_task: str,
                         agent_dir: Path, session_out: list | None = None,
                         resume_session: str | None = None) -> str:
    spec = SPECIALISTS[agent_key]
    system = (
        spec["system"]
        + " You are working autonomously — never ask questions. Write every deliverable as a real, "
        "complete file in your current working directory using the Write tool. Do not leave "
        "placeholders or TODOs. When finished, briefly summarize what you built."
    )
    if resume_session:
        prompt = (
            "Continue your assignment where you left off. Check what files already exist, then "
            "finish any incomplete or missing work. When done, briefly summarize what you completed."
        )
    else:
        prompt = (
            f"Overall project: {original_task}\n\n"
            f"Your assignment ({spec['title']}):\n{instruction}\n\n"
            "Create the complete files now, then summarize."
        )
    agent_dir.mkdir(parents=True, exist_ok=True)
    return await _run(system, prompt, cwd=agent_dir, allowed_tools=FILE_TOOLS,
                      max_turns=40, session_out=session_out, resume_session=resume_session)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def orchestrate(job: dict, on_update) -> None:
    task = job["task"]
    project_dir = OUTPUT_ROOT / job["project_slug"]
    project_dir.mkdir(parents=True, exist_ok=True)

    # --- Phase 1: Manager plans -------------------------------------------
    job["status"] = "planning"
    job["agents"]["manager"]["status"] = "working"
    job["agents"]["manager"]["started_at"] = time.time()
    job["agents"]["manager"]["ended_at"] = None
    on_update()

    try:
        plan = await run_manager(task)
    except Exception as e:  # noqa: BLE001
        job["agents"]["manager"]["status"] = "error"
        job["agents"]["manager"]["notes"] = str(e)
        job["agents"]["manager"]["ended_at"] = time.time()
        job["status"] = "error"
        job["error"] = f"Manager failed: {e}"
        on_update()
        return

    job["plan"] = plan
    job["agents"]["manager"]["status"] = "done"
    job["agents"]["manager"]["ended_at"] = time.time()
    job["agents"]["manager"]["notes"] = plan.get("summary", "")

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
        agent_dir = project_dir / key
        a = job["agents"][key]
        a["status"] = "working"
        a["instruction"] = t["instruction"]
        a["started_at"] = time.time()
        a["ended_at"] = None
        on_update()
        sid_out: list = []
        try:
            notes = await run_specialist(key, t["instruction"], task, agent_dir,
                                         session_out=sid_out)
            if sid_out:
                a["session_id"] = sid_out[0]
            a["status"] = "done"
            a["notes"] = notes[:600]
            a["files"] = scan_files(agent_dir)
            if not a["files"]:
                a["status"] = "error"
                a["notes"] = "Agent finished but wrote no files."
        except Exception as e:  # noqa: BLE001
            if sid_out:
                a["session_id"] = sid_out[0]
            a["status"] = "error"
            a["notes"] = str(e)
        a["ended_at"] = time.time()
        on_update()

    await asyncio.gather(*(do_one(t) for t in plan.get("tasks", [])))

    job["output_dir"] = str(project_dir)
    recompute_status(job)
    on_update()


def recompute_status(job: dict) -> None:
    """Set the overall job status from the individual agent states."""
    active = [a for a in job["agents"].values() if a["status"] != "skipped"]
    if any(a["status"] == "working" for a in active):
        job["status"] = "building"
        job["ended_at"] = None
    else:
        job["status"] = "done"  # finished; individual desks may still show 'error'
        if not job.get("ended_at"):
            job["ended_at"] = time.time()


async def resume_agent(job: dict, agent_key: str, on_update) -> None:
    """Continue a single specialist from its prior session (the Continue button)."""
    project_dir = OUTPUT_ROOT / job["project_slug"]
    agent_dir = project_dir / agent_key
    a = job["agents"][agent_key]

    a["status"] = "working"
    a["notes"] = ""
    a["started_at"] = time.time()
    a["ended_at"] = None
    job["status"] = "building"
    job["ended_at"] = None
    on_update()

    sid_out: list = [a["session_id"]] if a.get("session_id") else []
    try:
        notes = await run_specialist(
            agent_key, a.get("instruction", ""), job["task"], agent_dir,
            session_out=sid_out, resume_session=a.get("session_id"),
        )
        if sid_out:
            a["session_id"] = sid_out[0]
        a["status"] = "done"
        a["notes"] = notes[:600]
        a["files"] = scan_files(agent_dir)
        if not a["files"]:
            a["status"] = "error"
            a["notes"] = "Agent finished but wrote no files."
    except Exception as e:  # noqa: BLE001
        if sid_out:
            a["session_id"] = sid_out[0]
        a["status"] = "error"
        a["notes"] = str(e)
    a["ended_at"] = time.time()
    recompute_status(job)
    on_update()


def new_agents_state() -> dict:
    state = {
        "manager": {
            "title": MANAGER["title"], "emoji": MANAGER["emoji"],
            "status": "idle", "notes": "", "files": [], "instruction": "",
            "session_id": None, "started_at": None, "ended_at": None,
        }
    }
    for key, spec in SPECIALISTS.items():
        state[key] = {
            "title": spec["title"], "emoji": spec["emoji"],
            "status": "idle", "notes": "", "files": [], "instruction": "",
            "session_id": None, "started_at": None, "ended_at": None,
        }
    return state
