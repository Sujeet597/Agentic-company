/* Agentic Studio — frontend controller.
   Posts a work order, then polls the job and re-renders the studio floor. */

const SPECIALIST_ORDER = ["frontend", "backend", "ui_designer", "content_writer"];
const ROLE_LABELS = {
  frontend: "FRONTEND",
  backend: "BACKEND",
  ui_designer: "DESIGN",
  content_writer: "CONTENT",
};

const els = {
  form: document.getElementById("taskForm"),
  task: document.getElementById("taskInput"),
  project: document.getElementById("projectName"),
  btn: document.getElementById("dispatchBtn"),
  floor: document.getElementById("floor"),
  grid: document.getElementById("deskGrid"),
  runMeta: document.getElementById("runMeta"),
  runTimer: document.getElementById("runTimer"),
  healthDot: document.getElementById("healthDot"),
  healthText: document.getElementById("healthText"),
};

let pollTimer = null;
let currentJobId = null;

// Latest job snapshot + clock offset, so a 1s ticker can render live timers
// between polls without drifting from the server clock.
let latest = { job: null, offset: 0 };

function fmtDur(seconds) {
  if (seconds == null || seconds < 0 || !isFinite(seconds)) return "";
  const s = Math.floor(seconds);
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

// Live server time ≈ client now minus the measured offset.
function serverNow() {
  return Date.now() / 1000 - latest.offset;
}

function agentElapsed(a) {
  if (!a || !a.started_at) return null;
  const end = a.ended_at || serverNow();
  return end - a.started_at;
}

// --- Health check ----------------------------------------------------------
async function checkHealth() {
  try {
    const r = await fetch("/api/health");
    const d = await r.json();
    if (d.ok) {
      els.healthDot.className = "dot ok";
      els.healthText.textContent = "team online · via Claude Code";
    } else {
      els.healthDot.className = "dot bad";
      els.healthText.textContent = "backend not ready";
    }
  } catch {
    els.healthDot.className = "dot bad";
    els.healthText.textContent = "backend offline";
  }
}
checkHealth();

// --- Desk scaffolding ------------------------------------------------------
function buildDesks() {
  els.grid.innerHTML = "";
  const tpl = document.getElementById("deskTemplate");
  for (const key of SPECIALIST_ORDER) {
    const node = tpl.content.firstElementChild.cloneNode(true);
    node.id = `desk-${key}`;
    node.querySelector(".role").textContent = ROLE_LABELS[key];
    els.grid.appendChild(node);
  }
}

const STATUS_TEXT = {
  idle: "idle",
  working: "working",
  done: "done",
  error: "error",
  skipped: "not needed",
};

function renderAgent(key, a) {
  const desk = document.getElementById(`desk-${key}`);
  if (!desk) return;
  desk.dataset.status = a.status;

  const setText = (sel, txt) => { const n = desk.querySelector(sel); if (n) n.textContent = txt; };
  // Manager desk uses id-based nodes; specialist desks use class-based.
  const avatar = desk.querySelector(".avatar");
  const title = desk.querySelector(".desk-title, h3");
  const pill = desk.querySelector(".pill");
  const notes = desk.querySelector(".notes");

  if (avatar) avatar.textContent = a.emoji;
  if (title) title.textContent = a.title;
  if (pill) pill.textContent = STATUS_TEXT[a.status] || a.status;
  if (notes) notes.textContent = a.notes || "";

  // Per-desk timer lives next to the role label.
  const deskId = desk.querySelector(".desk-id");
  if (deskId && !desk.querySelector(".timer")) {
    const t = document.createElement("span");
    t.className = "timer mono";
    deskId.appendChild(t);
  }
  paintTimer(desk.querySelector(".timer"), a);

  const filesEl = desk.querySelector(".files");
  if (filesEl) {
    filesEl.innerHTML = "";
    (a.files || []).forEach((f) => {
      const li = document.createElement("li");
      li.textContent = f;
      filesEl.appendChild(li);
    });
  }

  // Continue button — shown only on an errored specialist desk.
  let btn = desk.querySelector(".continue-btn");
  if (a.status === "error" && key !== "manager") {
    if (!btn) {
      btn = document.createElement("button");
      btn.className = "continue-btn";
      btn.dataset.agent = key;
      btn.textContent = "Continue ▸";
      desk.appendChild(btn);
    }
    btn.disabled = false;
    btn.textContent = "Continue ▸";
  } else if (btn) {
    btn.remove();
  }
}

function paintTimer(el, a) {
  if (!el) return;
  const secs = agentElapsed(a);
  if (secs == null || a.status === "idle" || a.status === "skipped") {
    el.textContent = "";
    return;
  }
  const icon = a.status === "working" ? "⏱" : a.status === "error" ? "✕" : "✓";
  el.textContent = `${icon} ${fmtDur(secs)}`;
}

function jobElapsed(job) {
  if (!job || !job.created_at) return null;
  const end = job.status === "done" && job.ended_at ? job.ended_at : serverNow();
  return end - job.created_at;
}

// Update just the timer text — cheap, runs every second between polls.
function tickTimers() {
  const job = latest.job;
  if (!job) return;
  for (const key of ["manager", ...SPECIALIST_ORDER]) {
    const desk = document.getElementById(`desk-${key}`);
    if (desk) paintTimer(desk.querySelector(".timer"), job.agents[key]);
  }
  const total = jobElapsed(job);
  if (els.runTimer && total != null) {
    const running = job.status === "planning" || job.status === "building";
    els.runTimer.textContent = `${running ? "⏱" : "✓"} total ${fmtDur(total)}`;
  }
}

function render(job) {
  // Sync the clock offset from the server timestamp for drift-free timers.
  if (job.server_now) latest.offset = Date.now() / 1000 - job.server_now;
  latest.job = job;

  els.floor.dataset.live = job.status === "building" || job.status === "planning" ? "1" : "0";
  renderAgent("manager", job.agents.manager);
  for (const key of SPECIALIST_ORDER) renderAgent(key, job.agents[key]);

  const label = {
    queued: "queued…",
    planning: "manager is planning…",
    building: "team is building…",
    done: "delivered ✓",
    error: "run failed",
  }[job.status] || job.status;
  els.runMeta.textContent = `${job.project_slug}  ·  ${label}`;
  tickTimers();

  const anyWorking = Object.values(job.agents).some((a) => a.status === "working");
  if ((job.status === "done" || job.status === "error") && !anyWorking) {
    stopPolling();
    els.btn.disabled = false;
    els.btn.querySelector(".dispatch-label").textContent = "Dispatch to team";
    if (job.status === "error" && job.error) toast(job.error);
  }
}

// Smooth 1-second ticker for the live timers (resynced by each poll).
setInterval(tickTimers, 1000);

// --- Polling ---------------------------------------------------------------
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

function startPolling(jobId) {
  currentJobId = jobId;
  stopPolling();
  const tick = async () => {
    try {
      const r = await fetch(`/api/jobs/${jobId}`);
      if (!r.ok) throw new Error("Lost the job.");
      render(await r.json());
    } catch (e) {
      stopPolling();
      toast(e.message);
      els.btn.disabled = false;
    }
  };
  tick();
  pollTimer = setInterval(tick, 1500);
}

// --- Submit ----------------------------------------------------------------
els.form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const task = els.task.value.trim();
  if (!task) { toast("Write a task first."); return; }

  els.btn.disabled = true;
  els.btn.querySelector(".dispatch-label").textContent = "Dispatching…";
  els.floor.hidden = false;
  buildDesks();
  els.floor.scrollIntoView({ behavior: "smooth", block: "start" });

  try {
    const r = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task, project_name: els.project.value.trim() || "untitled-project" }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d.detail || "Could not start the run.");
    }
    const { job_id } = await r.json();
    startPolling(job_id);
  } catch (err) {
    toast(err.message);
    els.btn.disabled = false;
    els.btn.querySelector(".dispatch-label").textContent = "Dispatch to team";
  }
});

// --- Continue a failed agent -----------------------------------------------
els.grid.addEventListener("click", async (e) => {
  const btn = e.target.closest(".continue-btn");
  if (!btn || !currentJobId) return;
  const key = btn.dataset.agent;
  btn.disabled = true;
  btn.textContent = "Continuing…";
  try {
    const r = await fetch(`/api/jobs/${currentJobId}/agents/${key}/continue`, { method: "POST" });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d.detail || "Could not continue.");
    }
    const desk = document.getElementById(`desk-${key}`);
    if (desk) {
      desk.dataset.status = "working";
      const pill = desk.querySelector(".pill");
      if (pill) pill.textContent = "working";
    }
    startPolling(currentJobId);
  } catch (err) {
    toast(err.message);
    btn.disabled = false;
    btn.textContent = "Continue ▸";
  }
});

// --- Toast -----------------------------------------------------------------
let toastTimer = null;
function toast(msg) {
  document.querySelector(".toast")?.remove();
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  document.body.appendChild(t);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.remove(), 6000);
}
