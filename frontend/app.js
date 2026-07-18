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
  healthDot: document.getElementById("healthDot"),
  healthText: document.getElementById("healthText"),
};

let pollTimer = null;

// --- Health check ----------------------------------------------------------
async function checkHealth() {
  try {
    const r = await fetch("/api/health");
    const d = await r.json();
    if (d.has_key) {
      els.healthDot.className = "dot ok";
      els.healthText.textContent = "team online";
    } else {
      els.healthDot.className = "dot bad";
      els.healthText.textContent = "no API key — set ANTHROPIC_API_KEY";
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

  const filesEl = desk.querySelector(".files");
  if (filesEl) {
    filesEl.innerHTML = "";
    (a.files || []).forEach((f) => {
      const li = document.createElement("li");
      li.textContent = f;
      filesEl.appendChild(li);
    });
  }
}

function render(job) {
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

  if (job.status === "done" || job.status === "error") {
    stopPolling();
    els.btn.disabled = false;
    els.btn.querySelector(".dispatch-label").textContent = "Dispatch to team";
    if (job.status === "error" && job.error) toast(job.error);
  }
}

// --- Polling ---------------------------------------------------------------
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

function startPolling(jobId) {
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
