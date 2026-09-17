"use strict";

// ------------------------------------------------------------------ helpers
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
const api = (path, opts) => fetch(path, opts).then((r) => r.json());

function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(1)} ${u[i]}`;
}

function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
}

// ------------------------------------------------------------------ toasts (progress meters)
function showToast(id, text, pct) {
  const area = $("#toasts");
  if (!area) return null;
  let t = document.getElementById("toast-" + id);
  if (!t) {
    t = el("div", "toast");
    t.id = "toast-" + id;
    t.innerHTML = `<div class="toast-text"></div><div class="toast-meter"><i></i></div>`;
    area.appendChild(t);
  }
  t.classList.remove("hiding");
  $(".toast-text", t).textContent = text;
  $(".toast-meter > i", t).style.width = `${Math.max(0, Math.min(100, pct || 0))}%`;
  return t;
}
const updateToast = showToast;
function hideToast(id, finalText) {
  const t = document.getElementById("toast-" + id);
  if (!t) return;
  if (finalText) {
    $(".toast-text", t).textContent = finalText;
    $(".toast-meter > i", t).style.width = "100%";
  }
  t.classList.add("hiding");
  setTimeout(() => t.remove(), finalText ? 1400 : 200);
}

// ------------------------------------------------------------------ tabs
$$(".tab").forEach((t) => t.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.remove("active"));
  t.classList.add("active");
  const which = t.dataset.tab;
  $("#panel-run").classList.toggle("hidden", which !== "run");
  $("#panel-view").classList.toggle("hidden", which !== "view");
  if (which === "view") viewGrid.reload();
}));

// ------------------------------------------------------------------ grid controller
class GridController {
  constructor(mountId, countId, onSelectionChange) {
    this.mount = $(mountId);
    this.countEl = $(countId);
    this.onSelectionChange = onSelectionChange;
    this.selected = new Set();
    this.cells = [];
    this.dragging = false;
    this.dragMode = "add";
    document.addEventListener("mouseup", () => {
      if (this.dragging) { this.dragging = false; this._changed(); }
    });
  }

  // Stream the grid (NDJSON): fill cells as they arrive + show an X / N toast,
  // so a slow OneDrive scan shows live progress instead of a blank grid.
  async load(url) {
    const toastId = this.mount.id;
    this.cells = [];
    this.mount.innerHTML = `<div class="grid-msg">scanning folders…</div>`;
    showToast(toastId, "Scanning folders…", 0);
    let total = 0, done = 0, cleared = false;
    const present = new Set();
    try {
      const resp = await fetch(url);
      if (!resp.body || !resp.body.getReader) {           // fallback: no streaming
        const data = await resp.json();
        this.cells = data.cells || [];
        this.render();
        hideToast(toastId, `Loaded ${this.cells.length} folders`);
        return;
      }
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done: rdone } = await reader.read();
        if (rdone) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          let msg;
          try { msg = JSON.parse(line); } catch { continue; }
          if (msg.type === "total") {
            total = msg.total;
            updateToast(toastId, `Scanning 0 / ${total}…`, 0);
            if (!total) this.mount.innerHTML = `<div class="grid-msg">no CD* folders found — check the source path in ⚙</div>`;
          } else if (msg.type === "cell") {
            if (!cleared) { this.mount.innerHTML = ""; cleared = true; }
            this.cells.push(msg.cell);
            present.add(msg.cell.participant);
            this.mount.appendChild(this._makeCell(msg.cell));
            done++;
            if (total) updateToast(toastId, `Scanning ${done} / ${total}…`, (done / total) * 100);
          }
        }
      }
      [...this.selected].forEach((p) => { if (!present.has(p)) this.selected.delete(p); });
      this._updateCount();
      hideToast(toastId, total ? `Loaded ${done} / ${total} folders` : "No folders found");
      if (this.onLoaded) this.onLoaded(this.cells);
    } catch (e) {
      hideToast(toastId);
      this.mount.innerHTML = `<div class="grid-msg">couldn't load folders — check the paths in ⚙</div>`;
    }
  }
  reload() { if (this._url) this.load(this._url); }
  setUrl(u) { this._url = u; return this; }
  flagged() { return this.cells.filter((c) => c.flags && (c.flags.skipped || c.flags.warning)); }

  _makeCell(c) {
    const cell = el("div", `cell ${c.state}`, c.suffix);
    cell.dataset.participant = c.participant;
    cell.title = `${c.participant} — ${c.state} (${c.done ?? 0}/${c.total ?? 0})`;
    // Naming flags (Panel 1 only): a corner badge for problems that lose or
    // misidentify data. Info-level drift is left to the Naming check list.
    const f = c.flags;
    if (f && (f.skipped || f.warning)) {
      const sev = f.skipped ? "skipped" : "warning";
      cell.classList.add("has-flag");
      cell.appendChild(el("span", `fbadge ${sev}`, f.skipped ? "✕" : "!"));
      const parts = [];
      if (f.skipped) parts.push(`${f.skipped} file(s) skipped`);
      if (f.warning) parts.push(`${f.warning} naming warning(s)`);
      cell.title += ` — ${parts.join(", ")}`;
    }
    if (this.selected.has(c.participant)) cell.classList.add("selected");
    cell.addEventListener("mousedown", (e) => {
      e.preventDefault();
      this.dragging = true;
      this.dragMode = this.selected.has(c.participant) ? "remove" : "add";
      this._apply(c.participant, cell);
    });
    cell.addEventListener("mouseenter", () => {
      if (this.dragging) this._apply(c.participant, cell);
    });
    return cell;
  }

  render() {
    this.mount.innerHTML = "";
    if (this.onLoaded) setTimeout(() => this.onLoaded(this.cells), 0);
    const present = new Set(this.cells.map((c) => c.participant));
    [...this.selected].forEach((p) => { if (!present.has(p)) this.selected.delete(p); });
    this.cells.forEach((c) => this.mount.appendChild(this._makeCell(c)));
    this._updateCount();
  }

  _apply(participant, cell) {
    if (this.dragMode === "add") this.selected.add(participant);
    else this.selected.delete(participant);
    cell.classList.toggle("selected", this.selected.has(participant));
    this._updateCount();
  }
  _changed() { if (this.onSelectionChange) this.onSelectionChange([...this.selected]); }
  _updateCount() { if (this.countEl) this.countEl.textContent = `${this.selected.size} selected`; }
  clear() { this.selected.clear(); this.render(); this._changed(); }
  list() { return [...this.selected]; }
}

// ------------------------------------------------------------------ config / settings
let CONFIG = {};
api("/api/config").then((c) => { CONFIG = c; });

function renderPaths(s) {
  const row = (label, val, ok) => {
    if (!val || /unset-(source|output)-root$/.test(val)) {
      return `<div>${label}: <span class="warn">not set — open ⚙</span></div>`;
    }
    return `<div>${label}: ${val}${ok ? "" : ' <span class="warn">⚠ not found</span>'}</div>`;
  };
  $("#paths").innerHTML =
    row("source", s.source_root, s.source_exists) +
    row("output", s.output_root, s.output_exists);
}
async function loadSettings() {
  const s = await api("/api/settings");
  renderPaths(s);
  $("#set-source").value = s.source_root || "";
  $("#set-output").value = s.output_root || "";
  $("#set-step1").value = s.epoching_repo || "";
  $("#set-step2").value = s.sleep_metrics_repo || "";
  return s;
}
loadSettings();

$("#settings-btn").addEventListener("click", async () => {
  await loadSettings();
  $("#set-note").textContent = "";
  $("#settings-modal").classList.remove("hidden");
});
$("#set-cancel").addEventListener("click", () => $("#settings-modal").classList.add("hidden"));
$("#settings-modal").addEventListener("click", (e) => {
  if (e.target.id === "settings-modal") $("#settings-modal").classList.add("hidden");
});
$("#set-save").addEventListener("click", async () => {
  const body = {
    source_root: $("#set-source").value,
    output_root: $("#set-output").value,
    epoching_repo: $("#set-step1").value,
    sleep_metrics_repo: $("#set-step2").value,
  };
  const s = await api("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  renderPaths(s);
  const ok = (b) => (b ? "✓" : "✗");
  $("#set-note").innerHTML =
    `source ${ok(s.source_exists)} · output ${ok(s.output_exists)} · ` +
    `step 1 ${ok(s.epoching_exists)} · step 2 ${ok(s.sleep_metrics_exists)}` +
    (s.persisted === false ? " · could not persist" : " · saved for next session");
  runGrid.reload();
  viewGrid.reload();
});

// ================================================================== PANEL 1
const runGrid = new GridController("#grid-run", "#sel-count-run", (sel) => {
  if (sel.length === 1) loadSourceTree(sel[0]);
  else $("#filetree").innerHTML = `<div class="muted">Select a single cell to view its staging files.</div>`;
  loadFlags(sel);
}).setUrl("/api/panel1/grid-stream");
runGrid.onLoaded = () => updateFlagCount();
runGrid.load("/api/panel1/grid-stream");

// ---- naming flags ----------------------------------------------------------
const SEV_ORDER = { skipped: 0, warning: 1, info: 2 };
const SEV_LABEL = { skipped: "✕ skipped", warning: "! warning", info: "i info" };
let flagFilterOn = false;

function updateFlagCount() {
  const n = runGrid.flagged().length;
  $("#flag-count").textContent = n ? String(n) : "";
  $("#flag-filter").classList.toggle("has", n > 0);
  $("#flag-filter").title = n
    ? `${n} folder(s) have naming problems that skip or misidentify files. Click to show and select them.`
    : "No naming problems that skip or misidentify files.";
}

$("#flag-filter").addEventListener("click", () => {
  flagFilterOn = !flagFilterOn;
  $("#grid-run").classList.toggle("flag-filter", flagFilterOn);
  $("#flag-filter").classList.toggle("active", flagFilterOn);
  if (flagFilterOn) {
    // Select exactly the flagged folders, so CHECK / RUN act on them directly.
    runGrid.selected = new Set(runGrid.flagged().map((c) => c.participant));
    runGrid.render();
    runGrid._changed();
    logLine(`Showing ${runGrid.selected.size} flagged folder(s).`, "warning");
  }
});

let flagReq = 0;
async function loadFlags(participants) {
  const box = $("#flaglist");
  if (!participants.length) {
    $("#flag-scope").textContent = "";
    box.innerHTML = `<div class="muted">Select folders to see naming issues in their input files.</div>`;
    return null;
  }
  const mine = ++flagReq;
  $("#flag-scope").textContent = participants.length === 1
    ? `· ${participants[0]} · expected vs current`
    : `· ${participants.length} folders · issues`;
  box.innerHTML = `<div class="muted">checking names…</div>`;
  const data = await api("/api/flags/summary", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ participants }),
  }).catch(() => null);
  if (mine !== flagReq) return data;            // a newer selection superseded this
  if (participants.length === 1) {
    // One folder: show the convention beside every file, not just the problems.
    const view = await api(`/api/participant/${encodeURIComponent(participants[0])}/naming`).catch(() => null);
    if (mine !== flagReq) return data;
    renderConventions(box, view);
  } else {
    renderFlagList(box, data, { showInfo: false });
  }
  return data;
}

// Flag text comes from filenames and the rules file; patterns like "<Survey>"
// must render literally, not be parsed as HTML.
function esc(t) {
  return String(t ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

const STATUS_ICON = { ok: "✓", info: "i", warning: "!", skipped: "✕" };
const STATUS_TEXT = {
  ok: "matches convention", info: "name has drifted", warning: "check name", skipped: "not processed",
};

// Expected-vs-current for one participant: season -> device -> each file the
// pipeline uses, with the convention (from the naming sheet) shown above it.
function renderConventions(box, view) {
  box.innerHTML = "";
  if (!view || !view.exists) { box.appendChild(el("div", "muted", "Participant folder not found.")); return; }
  const c = view.counts || {};
  const head = el("div", "flag-counts");
  ["skipped", "warning", "info"].forEach((k) =>
    head.appendChild(el("span", `fcount ${k} ${c[k] ? "" : "zero"}`, `${SEV_LABEL[k]} <b>${c[k] || 0}</b>`)));
  box.appendChild(head);
  if (!view.seasons.length) {
    box.appendChild(el("div", "muted", "No files here that the pipeline processes."));
    return;
  }
  view.seasons.forEach((sn) => {
    const sb = el("div", "conv-season");
    sb.appendChild(el("div", "conv-shead",
      `<span class="cstat ${sn.status}">${STATUS_ICON[sn.status]}</span>` +
      `<span class="conv-sname">${esc(sn.name)}</span>` +
      `<span class="conv-exp">folder expected <code>${esc(sn.expected)}</code></span>`));
    sn.notes.forEach((n) => sb.appendChild(el("div", `conv-note ${n.severity}`, esc(n.message))));

    sn.devices.forEach((dv) => {
      const db = el("div", `conv-dev ${dv.status}`);
      db.appendChild(el("div", "conv-dhead",
        `<span class="conv-dname">${esc(dv.folder)}</span>` +
        `<span class="conv-exp">expected <code>${esc(dv.expected)}</code>` +
        (dv.example ? `<br>e.g. <code>${esc(dv.example)}</code>` : "") + `</span>`));
      dv.notes.forEach((n) => db.appendChild(el("div", `conv-note ${n.severity}`, esc(n.message))));
      dv.files.forEach((f) => {
        const row = el("div", `conv-file ${f.status}`);
        row.appendChild(el("span", `cstat ${f.status}`, STATUS_ICON[f.status]));
        const body = el("div", "conv-fbody");
        body.appendChild(el("div", "conv-fname", `<span class="conv-lbl">current</span> ${esc(f.name)}`));
        if (f.reasons.length) {
          f.reasons.forEach((r) => body.appendChild(el("div", `conv-reason ${r.severity}`, esc(r.message))));
        } else {
          body.appendChild(el("div", "conv-reason ok", STATUS_TEXT.ok));
        }
        row.appendChild(body);
        db.appendChild(row);
      });
      sb.appendChild(db);
    });
    box.appendChild(sb);
  });
}

function renderFlagList(box, data, { showInfo = false, limit = 200 } = {}) {
  box.innerHTML = "";
  if (!data) { box.appendChild(el("div", "muted", "Could not run the naming check.")); return; }
  const c = data.counts || {};
  const head = el("div", "flag-counts");
  ["skipped", "warning", "info"].forEach((k) =>
    head.appendChild(el("span", `fcount ${k} ${c[k] ? "" : "zero"}`, `${SEV_LABEL[k]} <b>${c[k] || 0}</b>`)));
  box.appendChild(head);

  const flags = (data.flags || [])
    .filter((f) => showInfo || f.severity !== "info")
    .sort((a, b) => SEV_ORDER[a.severity] - SEV_ORDER[b.severity]
      || a.participant.localeCompare(b.participant) || a.season.localeCompare(b.season));
  if (!flags.length) {
    box.appendChild(el("div", "muted", (c.info && !showInfo)
      ? `No problems that skip or misidentify files. ${c.info} cosmetic naming note(s) — select one folder to see them.`
      : "All processed input files follow the naming convention."));
    return;
  }
  flags.slice(0, limit).forEach((f) => {
    const row = el("div", `flag ${f.severity}`);
    const where = [f.participant, f.season, f.device].filter(Boolean).join(" / ");
    row.appendChild(el("div", "flag-top",
      `<span class="fsev ${f.severity}">${SEV_LABEL[f.severity]}</span><span class="fwhere">${esc(where)}</span>`));
    if (f.file) row.appendChild(el("div", "ffile", esc(f.file)));
    row.appendChild(el("div", "fmsg", esc(f.message)));
    if (f.expected) {
      row.appendChild(el("div", "fexp",
        `expected <code>${esc(f.expected)}</code>${f.example ? ` · e.g. <code>${esc(f.example)}</code>` : ""}`));
    }
    box.appendChild(row);
  });
  if (flags.length > limit) box.appendChild(el("div", "muted", `…and ${flags.length - limit} more.`));
}

$("#clear-run").addEventListener("click", () => runGrid.clear());

async function loadSourceTree(participant) {
  $("#fb-participant").textContent = "· " + participant;
  $("#filetree").innerHTML = `<div class="muted">loading…</div>`;
  const tree = await api(`/api/participant/${encodeURIComponent(participant)}/source`);
  if (!tree.exists) { $("#filetree").innerHTML = `<div class="muted">folder not found</div>`; return; }
  const box = el("div");
  tree.seasons.forEach((s) => {
    const sb = el("div", "season-blk");
    sb.appendChild(el("div", "s-name", s.name));
    (s.devices || []).forEach((d) => {
      const db = el("div", "dev-blk");
      db.appendChild(el("div", "d-name", d.name + (d.files.length ? "" : " (empty)")));
      d.files.forEach((f) => {
        const cloud = f.dehydrated
          ? `<span class="cloud" title="on OneDrive, not downloaded">☁ ${f.size_mb} MB</span>`
          : `<span class="local" title="local">● ${f.size_mb} MB</span>`;
        db.appendChild(el("div", "file", `<span>${f.name}</span>${cloud}`));
      });
      sb.appendChild(db);
    });
    box.appendChild(sb);
  });
  $("#filetree").innerHTML = "";
  $("#filetree").appendChild(box);
}

// ---- run + live progress
const runBtn = $("#run-btn");
// Three explicit actions instead of checkboxes that silently change what RUN does.
runBtn.addEventListener("click", () => startRun({ dryRun: false, force: false }));
$("#check-btn").addEventListener("click", () => startRun({ dryRun: true, force: false }));
$("#reprocess-btn").addEventListener("click", () => {
  const participants = runGrid.list();
  if (!participants.length) { logLine("Select at least one folder.", "warning"); return; }
  const shown = participants.slice(0, 12).join(", ") + (participants.length > 12 ? ` …(+${participants.length - 12})` : "");
  const ok = window.confirm(
    `Reprocess ${participants.length} folder(s)?\n\n${shown}\n\n` +
    "Everything is redone, including items already complete. Actigraph recordings " +
    "are re-downloaded from OneDrive (~1 GB each) and re-epoched, which can take a long time.");
  if (ok) startRun({ dryRun: false, force: true });
});

$("#stop-btn").addEventListener("click", async () => {
  if (!currentJobId) return;
  logLine("Stopping — terminating the running pipeline…", "warning");
  try { await fetch(`/api/jobs/${currentJobId}/stop`, { method: "POST" }); }
  catch (_) { logLine("Stop request failed.", "error"); }
});

function logLine(msg, level) {
  const log = $("#log-run");
  const line = el("div", "line " + (level || ""), msg);
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

let currentLabel = null;
let realJobActive = false;   // a non-dry run is currently in progress
let currentJobId = null;

// Progress cards: one per item, each holding a list of timed steps.
let progressCards = {};      // label -> { el, steps:{key:row}, badge }
let activeTimerEl = null;    // the .st-time of the currently-running step
let activeTimerStart = 0;
let timerHandle = null;

function ensureCard(label) {
  if (progressCards[label]) return progressCards[label];
  const card = el("div", "pitem");
  card.innerHTML =
    `<div class="p-label"><span class="p-name">${label}</span><span class="p-badge"></span></div>` +
    `<div class="steps"></div>`;
  $("#progress-list").appendChild(card);
  progressCards[label] = { el: card, steps: {}, badge: $(".p-badge", card) };
  return progressCards[label];
}
function stepRow(label, key) {
  const card = ensureCard(label);
  let row = card.steps[key];
  if (!row) {
    row = el("div", "stepline");
    row.innerHTML =
      `<span class="st-ic">▶</span><span class="st-name"></span><span class="st-time">0.0s</span>` +
      `<div class="st-detail"></div><div class="st-bar"><i></i></div>`;
    $(".steps", card.el).appendChild(row);
    card.steps[key] = row;
  }
  return row;
}
function startTimer(row) {
  activeTimerEl = $(".st-time", row);
  activeTimerStart = performance.now();
  if (!timerHandle) timerHandle = setInterval(() => {
    if (activeTimerEl) activeTimerEl.textContent = ((performance.now() - activeTimerStart) / 1000).toFixed(1) + "s";
  }, 100);
}
function stopTimer() { activeTimerEl = null; }
function killTimer() { if (timerHandle) { clearInterval(timerHandle); timerHandle = null; } activeTimerEl = null; }
function beginStep(label, key, name) {
  const row = stepRow(label, key);
  $(".st-name", row).textContent = name;
  $(".st-ic", row).textContent = "▶";
  row.classList.remove("done", "err");
  startTimer(row);
}
function endStep(label, key, seconds, ok = true) {
  const row = stepRow(label, key);
  $(".st-ic", row).textContent = ok ? "✓" : "✗";
  row.classList.add(ok ? "done" : "err");
  if (typeof seconds === "number") $(".st-time", row).textContent = seconds.toFixed(1) + "s";
  stopTimer();
}
function stepDetail(label, key, text) { $(".st-detail", stepRow(label, key)).textContent = text; }
function stepBar(label, key, pct) {
  const row = stepRow(label, key);
  row.classList.add("has-bar");
  $(".st-bar > i", row).style.width = `${pct || 0}%`;
}
function markSkip(label, reason) {
  setBadge(label, `<span class="pill">skip</span>`);
  const row = stepRow(label, "skip");
  $(".st-name", row).textContent = `skipped (${reason})`;
  $(".st-ic", row).textContent = "–";
  $(".st-time", row).textContent = "";
}
function setBadge(label, html) { ensureCard(label).badge.innerHTML = html; }
function showStop(on) { $("#stop-btn").style.display = on ? "" : "none"; }

async function startRun({ dryRun = false, force = false } = {}) {
  const participants = runGrid.list();
  if (!participants.length) { logLine("Select at least one folder.", "warning"); return; }
  const dry_run = dryRun;

  // Never disable the button. If a real run is busy, refuse new *real* runs but
  // still allow dry runs (which bypass the pipeline queue on the backend).
  if (!dry_run && realJobActive) {
    logLine("Pipeline busy — a run is already in progress. Wait for it to finish (dry runs are still allowed).", "warning");
    return;
  }

  // Pre-flight naming check: surfaced for CHECK, and a gate before a real run
  // when some input files would be skipped.
  const flags = await loadFlags(participants);
  const fc = (flags && flags.counts) || {};
  if (!dry_run && fc.skipped) {
    const skipped = (flags.flags || []).filter((f) => f.severity === "skipped");
    const list = skipped.slice(0, 8).map((f) => `• ${f.participant} / ${f.season}: ${f.file}`).join("\n");
    const go = window.confirm(
      `${fc.skipped} input file(s) in this selection can't be processed because of how ` +
      `they are named or placed:\n\n${list}${skipped.length > 8 ? "\n…" : ""}\n\n` +
      "Their results will be missing. Run anyway? (See Naming check on the right.)");
    if (!go) { logLine("Run cancelled — review the Naming check first.", "warning"); return; }
  }

  $("#log-run").innerHTML = "";
  $("#progress-list").innerHTML = "";
  progressCards = {}; currentLabel = null; killTimer();
  const mode = dry_run ? "CHECK — preview only, nothing is downloaded or written"
    : force ? "REPROCESS" : "RUN";
  logLine(`${mode}: ${participants.join(", ")}`, dry_run ? "warning" : "ok");
  if (flags) {
    const bits = [];
    if (fc.skipped) bits.push(`${fc.skipped} skipped`);
    if (fc.warning) bits.push(`${fc.warning} warning(s)`);
    if (fc.info) bits.push(`${fc.info} note(s)`);
    logLine(bits.length ? `Naming check: ${bits.join(", ")} — details on the right.`
                        : "Naming check: all input files follow the convention.",
            fc.skipped ? "error" : fc.warning ? "warning" : "ok");
  }

  const resp = await api("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ participants, force, dry_run }),
  });
  if (resp.error) { logLine("Error: " + resp.error, "error"); return; }
  if (!dry_run) realJobActive = true;
  currentJobId = resp.job_id;
  openJobSocket(resp.job_id, dry_run);
}

function setJobStatus(text, cls) {
  const p = $("#job-status");
  p.textContent = text;
  p.className = "pill " + (cls || "");
}

function openJobSocket(jobId, dryRun) {
  setJobStatus(dryRun ? "dry run" : "running", "running");
  const ws = new WebSocket(`ws://${location.host}/ws/jobs/${jobId}`);
  ws.onmessage = (ev) => handleEvent(JSON.parse(ev.data));
  ws.onclose = () => { if (!dryRun) realJobActive = false; };
  ws.onerror = () => { logLine("WebSocket error", "error"); if (!dryRun) realJobActive = false; };
}

function handleEvent(e) {
  switch (e.type) {
    case "job_start":
      showStop(true);
      logLine(`▶ job start: ${e.participants.join(", ")}${e.force ? " (force)" : ""}${e.dry_run ? " (dry run)" : ""}`, "ok");
      break;
    case "discovery":
      logLine(`discovered ${e.items.length} item(s) across seasons: ${(e.seasons || []).join(", ")}`);
      break;
    case "item_start":
      currentLabel = e.label; ensureCard(e.label);
      logLine(`— ${e.label}`);
      break;
    case "item_skip":
      markSkip(e.label, e.reason);
      logLine(`  skip: ${e.label} (${e.reason})`);
      break;
    case "download_start":
      if (currentLabel) beginStep(currentLabel, "download", `OneDrive download · ${fmtBytes(e.total_bytes)}`);
      break;
    case "download_progress":
      if (currentLabel) {
        stepBar(currentLabel, "download", e.pct);
        stepDetail(currentLabel, "download", `${e.pct.toFixed(1)}% · ${fmtBytes(e.downloaded_bytes)} / ${fmtBytes(e.total_bytes)}`);
      }
      break;
    case "download_done":
      if (currentLabel) { stepBar(currentLabel, "download", 100); endStep(currentLabel, "download", e.seconds); }
      break;
    case "step_start":
      if (currentLabel) beginStep(currentLabel, e.name, e.name);
      break;
    case "step_stdout":
      // Verbose line from the epoching / sleep-metrics tool. Always reflect the
      // latest line in that step's detail; only flood the log when verbose is on.
      if (currentLabel) stepDetail(currentLabel, e.name, e.line);
      if ($("#verbose").checked) logLine(`    ${e.name}| ${e.line}`);
      break;
    case "step_done":
      if (currentLabel) endStep(currentLabel, e.name, e.seconds);
      break;
    case "copy_start":
      if (currentLabel) beginStep(currentLabel, "copy", "copy → output");
      break;
    case "copy_progress":
      if (currentLabel) { stepBar(currentLabel, "copy", e.pct); stepDetail(currentLabel, "copy", `${e.pct.toFixed(1)}%`); }
      break;
    case "copy_done":
      if (currentLabel) { stepBar(currentLabel, "copy", 100); endStep(currentLabel, "copy"); }
      break;
    case "compliance_result":
      setBadge(e.label, `<span class="verdict ${e.verdict}">${e.verdict}</span>`);
      logLine(`  compliance: ${e.verdict} — ${JSON.stringify(e.summary)}`, e.verdict === "PASS" ? "ok" : "warning");
      break;
    case "item_done":
      logLine(`  ✓ ${e.label}`, "ok");
      break;
    case "item_error":
      setBadge(e.label, `<span class="verdict ERROR">ERR</span>`);
      logLine(`  ✗ ${e.label}: ${e.error}`, "error");
      break;
    case "item_cancelled":
      setBadge(e.label, `<span class="verdict REVIEW">STOP</span>`);
      logLine(`  ⏹ ${e.label} cancelled: ${e.reason || ""}`, "warning");
      break;
    case "run_cancelled":
      logLine(`  run cancelled by user`, "warning");
      break;
    case "log":
      logLine(`  ${e.message}`, e.level);
      break;
    case "run_end":
      logLine(`participant run finished: ${JSON.stringify(e.counts)}`);
      break;
    case "job_end":
      if (!e.dry_run) realJobActive = false;   // free the button for the next real run
      killTimer(); showStop(false);
      setJobStatus(e.status, e.status === "done" ? "done" : "error");
      logLine(`■ ${e.dry_run ? "dry run" : "job"} ${e.status}${e.error ? ": " + e.error : ""}`, e.status === "done" ? "ok" : "warning");
      if (!e.dry_run) runGrid.reload();   // cells that finished turn white
      break;
  }
}

// ================================================================== PANEL 2
const viewGrid = new GridController("#grid-view", "#sel-count-view", (sel) => {
  clearDetail();   // selection changed — drop any report shown for the old folder
  if (sel.length === 1) loadDrilldown(sel[0]);
  else { $("#device-row").innerHTML = ""; $("#measures").innerHTML = `<div class="muted">Select one folder, then pick a device below.</div>`; }
}).setUrl("/api/panel2/grid-stream");

$("#clear-view").addEventListener("click", () => viewGrid.clear());
$("#agg-btn").addEventListener("click", runAggregate);

async function runAggregate() {
  const participants = viewGrid.list();
  if (!participants.length) { $("#agg").innerHTML = `<div class="muted">Select processed folders first.</div>`; return; }
  $("#agg").innerHTML = `<div class="muted">aggregating…</div>`;
  const a = await api("/api/panel2/aggregate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ participants }),
  });
  renderAggregate(a);
  renderAggregateFlags(participants);
}

// Panel 2: explain gaps in the aggregate. A season missing from the numbers above
// is often a file that was skipped, or processed under a fallback, because of its
// name - so say so right where the numbers are read.
async function renderAggregateFlags(participants) {
  const host = el("div", "agg-flags");
  host.appendChild(el("h3", "sub", "Input naming"));
  const body = el("div", "flaglist compact", `<div class="muted">checking input file names…</div>`);
  host.appendChild(body);
  $("#agg").appendChild(host);
  const data = await api("/api/flags/summary", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ participants }),
  }).catch(() => null);
  const c = (data && data.counts) || {};
  const lead = c.skipped
    ? `<b>${c.skipped} input file(s) could not be processed</b>, so their results are missing from the numbers above.`
    : c.warning
      ? `All input files were processed, but ${c.warning} were only found through a naming fallback or have an ID that doesn't match their folder.`
      : "";
  if (lead) host.insertBefore(el("div", c.skipped ? "agg-note bad" : "agg-note", lead), body);
  renderFlagList(body, data, { showInfo: false, limit: 12 });
}

function renderAggregate(a) {
  const box = el("div");
  const seasons = Object.entries(a.seasons_by_type || {}).map(([k, v]) => `${v} ${k.toLowerCase()}`).join(", ") || "—";
  const stats = el("div", "stat-row");
  stats.appendChild(el("div", "stat", `<div class="n">${a.n_participants}</div><div class="k">participants</div>`));
  stats.appendChild(el("div", "stat", `<div class="n">${a.n_season_datasets}</div><div class="k">season datasets</div>`));
  box.appendChild(stats);
  box.appendChild(el("div", "muted", `seasons: ${seasons}`));

  Object.entries(a.devices || {}).forEach(([dev, d]) => {
    box.appendChild(el("h3", "sub", dev));
    const row = el("div", "stat-row");
    row.appendChild(el("div", "stat", `<div class="n">${d.n_items}</div><div class="k">recordings</div>`));
    row.appendChild(el("div", "stat", `<div class="n">${d.passed}</div><div class="k">passed</div>`));
    row.appendChild(el("div", "stat", `<div class="n">${d.review}</div><div class="k">review</div>`));
    row.appendChild(el("div", "stat", `<div class="n">${d.failed}</div><div class="k">failed</div>`));
    row.appendChild(el("div", "stat", `<div class="n">${d.pct_compliance_mean ?? "—"}%</div><div class="k">mean compliance</div>`));
    box.appendChild(row);

    const tbl = el("table", "tbl");
    tbl.innerHTML = `<tr><th>season</th><th>recordings</th><th>passed</th><th>mean %compliance</th></tr>`;
    Object.entries(d.per_season || {}).forEach(([s, ps]) => {
      tbl.appendChild(el("tr", null, `<td>${s}</td><td>${ps.n}</td><td>${ps.passed}</td><td>${ps.pct_compliance_mean ?? "—"}%</td>`));
    });
    box.appendChild(tbl);
  });

  $("#agg").innerHTML = "";
  $("#agg").appendChild(box);

  // Make every recording in the selection pickable for Step 2 drill-down
  // (so a single-folder selection is not required).
  populateDrilldownFromItems(a.items);
}

function populateDrilldownFromItems(items) {
  clearDetail();
  const row = $("#device-row");
  row.innerHTML = "";
  if (!items || !items.length) {
    $("#measures").innerHTML = `<div class="muted">no processed recordings in the selection</div>`;
    return;
  }
  $("#mv-title").textContent = `· ${items.length} recording(s)`;
  items.forEach((it) => {
    const b = el("button", "btn", `${it.participant} · ${it.season}`);
    b.addEventListener("click", () => showMeasures(it.participant, it));
    row.appendChild(b);
  });
  $("#measures").innerHTML = `<div class="muted">pick a recording above to see its Step 2 measures</div>`;
}

// ---- drilldown: pick folder -> device -> measures
let drillItems = [];
async function loadDrilldown(participant) {
  clearDetail();
  $("#mv-title").textContent = "· " + participant;
  $("#measures").innerHTML = `<div class="muted">loading…</div>`;
  const data = await api(`/api/participant/${encodeURIComponent(participant)}/output-items`);
  drillItems = data.items || [];
  const row = $("#device-row");
  row.innerHTML = "";
  if (!drillItems.length) { $("#measures").innerHTML = `<div class="muted">no processed items for ${participant}</div>`; return; }
  drillItems.forEach((it, i) => {
    const b = el("button", "btn", `${it.season} · ${it.device}`);
    b.addEventListener("click", () => showMeasures(participant, it));
    row.appendChild(b);
  });
  $("#measures").innerHTML = `<div class="muted">pick a season · device above</div>`;
}

async function showMeasures(participant, it) {
  clearDetail();   // picking a different recording/device resets the report viewer
  $("#measures").innerHTML = `<div class="muted">loading…</div>`;
  const q = new URLSearchParams({ season: it.season, device: it.device, stem: it.stem });
  const m = await api(`/api/participant/${encodeURIComponent(participant)}/measures?${q}`);
  const box = el("div");
  const c = m.compliance ? m.compliance.summary : null;
  if (c) {
    box.appendChild(el("h3", "sub", `compliance — ${m.compliance.verdict}`));
    box.appendChild(kvTable({
      "valid days": `${c.valid_days}/${c.total_days}`,
      "non-wear": c.nonwear_pct != null ? c.nonwear_pct + "%" : "—",
      "mean %compliance": c.pct_compliance_mean != null ? c.pct_compliance_mean + "%" : "—",
      "detection": c.wear_detection || "—",
    }));
  }
  if (m.outputs && m.outputs.length) {
    box.appendChild(el("h3", "sub", "measures & reports — click to view below"));
    const list = el("div", "out-links");
    m.outputs.forEach((o) => {
      const a = el("a", "out-link " + o.kind, `${o.kind === "pdf" ? "📄" : "📊"} ${o.label}`);
      a.href = "#";
      a.addEventListener("click", (e) => { e.preventDefault(); openDetail(participant, it, o); });
      list.appendChild(a);
    });
    box.appendChild(list);
  } else {
    box.appendChild(el("div", "muted", "no measure / report files found for this recording"));
  }
  $("#measures").innerHTML = "";
  $("#measures").appendChild(box);
}

function outputUrl(participant, it, name) {
  const q = new URLSearchParams({ participant, season: it.season, device: it.device, name });
  return `/api/output-file?${q}`;
}

function clearDetail() {
  // Reset the bottom report/measure viewer so a previously-opened report never
  // lingers after the participant / device selection changes.
  const title = $("#detail-title");
  if (title) title.textContent = "Report / measure viewer";
  const open = $("#detail-open");
  if (open) open.innerHTML = "";
  const body = $("#detail-body");
  if (body) body.innerHTML = `<div class="muted">Pick a recording above, then click a measure or report to view it here.</div>`;
}

async function openDetail(participant, it, o) {
  $("#detail-title").textContent = `${participant} · ${it.season} — ${o.label}`;
  const url = outputUrl(participant, it, o.name);
  $("#detail-open").innerHTML = `<a href="${url}" target="_blank" rel="noopener">↗ open in new tab</a>`;
  const body = $("#detail-body");
  body.innerHTML = `<div class="muted">loading…</div>`;
  $("#detail-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
  if (o.kind === "pdf") {
    body.innerHTML = `<iframe class="pdf-frame" src="${url}" title="${o.label}"></iframe>`;
    return;
  }
  const q = new URLSearchParams({ participant, season: it.season, device: it.device, name: o.name });
  const data = await api(`/api/output-csv?${q}`);
  if (data.error) { body.innerHTML = `<div class="muted">could not load: ${data.error}</div>`; return; }
  body.innerHTML = "";
  body.appendChild(csvTable(data.columns, data.rows));
  if (data.truncated) body.appendChild(el("div", "muted", "showing first 3000 rows"));
}

function csvTable(cols, rows) {
  const wrap = el("div", "tbl-scroll");
  const t = el("table", "tbl");
  t.appendChild(el("tr", null, cols.map((c) => `<th>${c}</th>`).join("")));
  rows.forEach((r) => t.appendChild(el("tr", null, cols.map((c) => `<td>${r[c] == null ? "" : r[c]}</td>`).join(""))));
  wrap.appendChild(t);
  return wrap;
}

function kvTable(obj) {
  const t = el("table", "tbl");
  Object.entries(obj).forEach(([k, v]) => t.appendChild(el("tr", null, `<th>${k}</th><td>${v}</td>`)));
  return t;
}
function recordsTable(rows) {
  const t = el("table", "tbl");
  const cols = Object.keys(rows[0]);
  t.appendChild(el("tr", null, cols.map((c) => `<th>${c}</th>`).join("")));
  rows.forEach((r) => t.appendChild(el("tr", null, cols.map((c) => `<td>${r[c]}</td>`).join(""))));
  return t;
}

// ------------------------------------------------------------------ shutdown
$("#shutdown").addEventListener("click", async () => {
  if (!confirm("Stop the dashboard server and free the port?")) return;
  try { await fetch("/api/shutdown", { method: "POST" }); } catch (_) {}
  document.body.innerHTML =
    '<div style="padding:40px;font:16px system-ui;color:#8b94a7">' +
    "Server stopped. You can close this tab.</div>";
});

// ------------------------------------------------------------------ startup / update panel
// Shown before the dashboard: reports whether this app and each linked tool repo
// is behind its GitHub remote, and can fast-forward them. Auto-continues when
// everything is current (or when git/network is unavailable) so it never blocks.
let startupDismissed = false;
// Outcome of the last update per component. The list re-renders after an
// update, so without this the "updated" confirmation would vanish instantly.
const recentResults = new Map();   // key -> {msg, cls}

function noteResult(key, msg, cls) {
  recentResults.set(key, { msg, cls });
  setTimeout(() => { recentResults.delete(key); }, 8000);
}

function dismissStartup() {
  if (startupDismissed) return;
  startupDismissed = true;
  const el0 = $("#startup");
  if (el0) el0.classList.add("hidden");
}

function compRow(c) {
  const row = el("div", "comp");
  const stateLabel = {
    current: "up to date",
    behind: c.behind === 1 ? "1 update" : `${c.behind} updates`,
    missing: "not installed", "not-git": "no git repo",
    offline: "offline", "no-git": "git missing", "no-upstream": "no remote",
  }[c.state] || c.state;
  const pillClass = c.update_available ? "behind"
    : c.state === "current" ? "current"
    : c.state === "missing" ? "error" : c.state;

  const left = el("div");
  left.appendChild(el("div", "c-name", c.name));
  left.appendChild(el("div", "c-role",
    `${c.role || ""}${c.commit ? " · " + c.commit : ""}${c.branch ? " (" + c.branch + ")" : ""}`));
  if (c.message) left.appendChild(el("div", "c-msg", c.message));
  row.appendChild(left);
  row.appendChild(el("span", "c-pill " + pillClass, stateLabel));

  const slot = el("div", "c-actions");
  if (c.update_available) {
    // Users of this dashboard consume the tool repos; they never contribute to
    // them. So there is exactly one action - get the published version - and it
    // always works, whatever state the folder is in.
    const b = el("button", "btn", "Update");
    b.title = "Download the latest published version of this tool";
    b.addEventListener("click", () => updateComponent(c.key, b));
    slot.appendChild(b);
  }
  row.appendChild(slot);

  const past = recentResults.get(c.key);
  if (past) {
    const note = el("div", "c-result " + (past.cls || ""), past.msg);
    row.appendChild(note);
  }

  // The dashboard's own folder is fixed; every tool repo can live anywhere.
  if (c.key !== "cd-compliance-checks") {
    const pathRow = el("div", "comp-path");
    const input = el("input");
    input.type = "text"; input.spellcheck = false;
    input.value = c.path || "";
    input.title = "Folder this tool lives in";
    pathRow.appendChild(input);

    const use = el("button", "btn ghost", "Use folder");
    use.title = "Point the dashboard at this existing folder";
    use.addEventListener("click", async () => {
      use.disabled = true; use.textContent = "Saving…";
      await api("/api/components/path", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: c.key, path: input.value }),
      }).catch(() => null);
      await loadComponents(false);
    });
    pathRow.appendChild(use);

    if (c.state === "missing" || c.state === "not-git") {
      const inst = el("button", "btn", "Install here");
      inst.title = "Download this tool from GitHub into the folder shown";
      inst.addEventListener("click", async () => {
        inst.disabled = true; inst.textContent = "Installing…";
        const r = await api("/api/components/install", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ key: c.key, path: input.value }),
        }).catch(() => ({ ok: false, error: "request failed" }));
        if (!r.ok) $("#startup-sub").textContent = `${c.name}: ${r.error || "install failed"}`;
        await loadComponents(false);
      });
      pathRow.appendChild(inst);
    }
    row.appendChild(pathRow);
  }
  return row;
}

async function updateComponent(key, btn) {
  const row = btn ? btn.closest(".comp") : null;
  const say = (msg, cls) => {
    if (!row) return;
    let note = $(".c-result", row);
    if (!note) { note = el("div", "c-result"); row.appendChild(note); }
    note.className = "c-result " + (cls || "");
    note.textContent = msg;
  };
  if (btn) { btn.disabled = true; btn.textContent = "Updating…"; }
  say("updating…");
  try {
    const r = await api("/api/components/update", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    });
    if (r.ok) {
      const moved = r.status && r.status.commit ? ` → ${r.status.commit}` : "";
      say("updated" + moved, "ok");
      noteResult(key, "updated" + moved, "ok");
      if (r.restart_required) {
        $("#startup-sub").innerHTML =
          "<b>Dashboard updated — restart the app to load the new version.</b>";
      }
    } else {
      say(r.error || "update failed", "err");
      noteResult(key, r.error || "update failed", "err");
    }
  } catch (e) {
    say("update failed — see the terminal", "err");
    noteResult(key, "update failed — see the terminal", "err");
  }
  await loadComponents(false);
}

async function loadComponents(fetchRemote = true) {
  const list = $("#comp-list");
  if (!list) return;
  list.innerHTML = `<div class="muted" style="padding:8px">checking…</div>`;
  let data;
  try {
    data = await api(`/api/components?fetch=${fetchRemote ? 1 : 0}`);
  } catch (e) {
    $("#startup-sub").textContent =
      "Could not check versions (offline). The dashboard still works.";
    list.innerHTML = "";
    return;
  }
  const comps = data.components || [];
  list.innerHTML = "";
  comps.forEach((c) => list.appendChild(compRow(c)));

  const pending = comps.filter((c) => c.update_available);
  const updatable = pending.filter((c) => c.state !== "behind-dirty");
  $("#startup-updateall").style.display = pending.length > 1 ? "" : "none";

  // Never auto-launch: this is a start page, so the user decides when to enter
  // and can set where each tool lives first.
  const missing = comps.filter((c) => c.state === "missing" || c.state === "not-git");
  if (!data.git) {
    $("#startup-sub").textContent =
      "git is not installed, so versions cannot be checked. You can still use the dashboard.";
  } else if (missing.length) {
    $("#startup-sub").innerHTML =
      `<b>${missing.length} tool(s) are not installed.</b> Choose a folder and press ` +
      `“Install here”, or continue without them.`;
  } else if (pending.length) {
    $("#startup-sub").innerHTML =
      `<b>${pending.length} tool(s) have a newer version available.</b> ` +
      "Press Update to get it, or continue with what you have.";
  } else {
    $("#startup-sub").textContent = "All components are installed and up to date.";
  }
}

$("#startup-continue")?.addEventListener("click", dismissStartup);
$("#startup-recheck")?.addEventListener("click", () => loadComponents(true));
$("#startup-updateall")?.addEventListener("click", async () => {
  const btn = $("#startup-updateall");
  btn.disabled = true; btn.textContent = "Updating…";
  const data = await api("/api/components?fetch=0").catch(() => ({ components: [] }));
  for (const c of (data.components || []).filter((x) => x.update_available)) {
    await updateComponent(c.key, null);
  }
  btn.disabled = false; btn.textContent = "Update all";
});

loadComponents(true);


// ------------------------------------------------------------------ branding
// The supplied CHIP-D logo is used verbatim when webapp/static/logo.png exists;
// until then the drawn SVG mark stands in. Nothing here modifies the image.
(function applyBrandLogo() {
  const src = "/static/logo.png";
  const probe = new Image();
  probe.onload = () => {
    document.querySelectorAll(".brand .logo, .mark-glow .mark").forEach((node) => {
      const img = document.createElement("img");
      img.src = src;
      img.alt = "CHIP-D";
      // NB: an <svg>'s .className is an SVGAnimatedString, not a string,
      // so read the attribute to carry .logo / .mark styling across.
      img.setAttribute("class", node.getAttribute("class") || "");
      node.replaceWith(img);
    });
    const link = document.querySelector("link[rel='icon']");
    if (link) link.href = src;             // same image in the browser tab
  };
  probe.onerror = () => {};                // keep the fallback mark
  probe.src = src;
})();
