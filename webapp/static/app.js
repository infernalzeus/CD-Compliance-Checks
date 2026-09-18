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
  $("#panel-t2").classList.toggle("hidden", which !== "t2");
  if (which === "view") viewGrid.reload();
  if (which === "t2") loadT2Selections();
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
// A path that does not exist is the usual cause of an "empty" dashboard (a
// renamed folder, a drive that is not mapped), so say so beside the field.
function markSettingField(sel, exists, value) {
  const input = $(sel);
  if (!input) return;
  let note = input.nextElementSibling;
  if (!note || !note.classList.contains("set-status")) {
    note = el("div", "set-status");
    input.after(note);
  }
  const unset = !value || /unset-(source|output)-root$/.test(value);
  input.classList.toggle("bad", !unset && !exists);
  note.className = `set-status ${unset ? "" : exists ? "ok" : "bad"}`;
  note.textContent = unset ? "not set yet" : exists ? "✓ folder found" : "✗ folder not found — check the path (was it renamed or moved?)";
}

async function loadSettings() {
  const s = await api("/api/settings");
  renderPaths(s);
  $("#set-source").value = s.source_root || "";
  $("#set-output").value = s.output_root || "";
  $("#set-t2").value = s.t2_root || "";
  $("#set-step1").value = s.epoching_repo || "";
  $("#set-step2").value = s.sleep_metrics_repo || "";
  $("#set-file").innerHTML = s.settings_file
    ? `Saved for you in: ${esc(s.settings_file)}` +
      (s.settings_saved_at ? ` <span class="muted">(last written ${esc(s.settings_saved_at.replace("T", " "))})</span>`
                           : ` <span class="warn">— nothing saved yet from this computer</span>`)
    : "";
  markSettingField("#set-source", s.source_exists, s.source_root);
  markSettingField("#set-output", s.output_exists, s.output_root);
  markSettingField("#set-t2", s.t2_exists, s.t2_root);
  markSettingField("#set-step1", s.epoching_exists, s.epoching_repo);
  markSettingField("#set-step2", s.sleep_metrics_exists, s.sleep_metrics_repo);
  if (s.initials && !$("#initials").value) $("#initials").value = s.initials;
  return s;
}
loadSettings().then((s) => {
  const missing = [];
  if (s.source_root && !s.source_exists && !/unset-source-root$/.test(s.source_root)) missing.push("source");
  if (s.output_root && !s.output_exists && !/unset-output-root$/.test(s.output_root)) missing.push("output");
  if (missing.length) {
    showToast("paths", `${missing.join(" and ")} folder not found — open ⚙ to fix the path`, 100);
    setTimeout(() => hideToast("paths"), 8000);
  }
});

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
    t2_root: $("#set-t2").value,
    epoching_repo: $("#set-step1").value,
    sleep_metrics_repo: $("#set-step2").value,
  };
  const s = await api("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  renderPaths(s);
  markSettingField("#set-source", s.source_exists, s.source_root);
  markSettingField("#set-output", s.output_exists, s.output_root);
  markSettingField("#set-t2", s.t2_exists, s.t2_root);
  markSettingField("#set-step1", s.epoching_exists, s.epoching_repo);
  markSettingField("#set-step2", s.sleep_metrics_exists, s.sleep_metrics_repo);
  const note = $("#set-note");
  if (s.persisted === false) {
    note.className = "modal-note save-bad";
    note.innerHTML = `<b>NOT SAVED.</b> ${esc(s.settings_error || "the settings file could not be written")}` +
      `<br>Tried: ${esc(s.settings_file || "")}`;
  } else {
    note.className = "modal-note save-ok";
    note.innerHTML = `<b>Updated path.</b> Saved to ${esc(s.settings_file || "")}`;
    $("#set-file").innerHTML = `Saved for you in: ${esc(s.settings_file || "")}` +
      (s.settings_saved_at ? ` <span class="muted">(last written ${esc(s.settings_saved_at.replace("T", " "))})</span>` : "");
  }
  runGrid.reload();
  viewGrid.reload();
});

// ================================================================== PANEL 1
const runGrid = new GridController("#grid-run", "#sel-count-run", (sel) => {
  if (sel.length === 1) loadSourceTree(sel[0]);
  else $("#filetree").innerHTML = `<div class="muted">Select a single cell to view its staging files.</div>`;
  loadFlags(sel);
  updateRunPlan();
}).setUrl("/api/panel1/grid-stream");
runGrid.onLoaded = () => { updateFlagCount(); updateRunPlan(); };
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

// ---- run mode: one RUN button whose job is set by an explicit mode --------
// new     -> process new samples and anything unfinished; complete items skipped
// preview -> dry run: plan + naming check, nothing downloaded or written
// force   -> redo everything selected, including complete items
const RUN_MODES = {
  new:     { label: "RUN<br>CHECKS",  tag: "RUN · new & unfinished", cls: "" },
  preview: { label: "PREVIEW<br>(dry run)", tag: "PREVIEW · dry run", cls: "preview" },
  force:   { label: "FORCE<br>REPROCESS", tag: "FORCE REPROCESS", cls: "force" },
};
function runMode() { return ($("input[name=runmode]:checked") || {}).value || "new"; }

// What RUN will do to the current selection, from the grid's own counts:
// total = items with input, done = items whose outputs already exist.
function updateRunPlan() {
  const mode = runMode(), m = RUN_MODES[mode];
  runBtn.innerHTML = m.label;
  runBtn.className = `btn run ${m.cls}`;
  const sel = new Set(runGrid.list());
  const cells = runGrid.cells.filter((c) => sel.has(c.participant));
  const plan = $("#run-plan");
  if (!cells.length) { plan.textContent = "Select folders to see what RUN will do."; return; }
  const total = cells.reduce((a, c) => a + (c.total || 0), 0);
  const done = cells.reduce((a, c) => a + (c.done || 0), 0);
  const todo = total - done;
  const skipped = cells.reduce((a, c) => a + ((c.flags && c.flags.skipped) || 0), 0);
  let txt;
  if (mode === "new") txt = todo
    ? `Processes <b>${todo}</b> new/unfinished item(s); skips <b>${done}</b> already complete.`
    : `Nothing to do: all <b>${done}</b> item(s) are complete. Use Force reprocess to redo them.`;
  else if (mode === "preview") txt =
    `Lists <b>${todo}</b> item(s) to process and <b>${done}</b> to skip. Nothing is downloaded or written.`;
  else txt = `Redoes all <b>${total}</b> item(s), including <b>${done}</b> already complete (~1 GB download per Actigraph recording).`;
  if (skipped) txt += ` <span class="plan-warn">${skipped} input file(s) can't be processed — see Naming check.</span>`;
  plan.innerHTML = txt;
}
document.querySelectorAll("input[name=runmode]").forEach((r) => r.addEventListener("change", updateRunPlan));

function currentInitials() {
  const v = ($("#initials").value || "").replace(/[^A-Za-z]/g, "").toUpperCase();
  return /^[A-Z]{2,4}$/.test(v) ? v : null;
}

runBtn.addEventListener("click", () => {
  const mode = runMode();
  if (mode !== "preview" && !currentInitials()) {
    logLine("Enter your initials (2-4 letters) before a run that writes data — it's recorded on the batch.", "warning");
    $("#initials").focus();
    $("#initials").classList.add("need");
    return;
  }
  if (mode === "force") {
    const participants = runGrid.list();
    if (!participants.length) { logLine("Select at least one folder.", "warning"); return; }
    const shown = participants.slice(0, 12).join(", ") + (participants.length > 12 ? ` …(+${participants.length - 12})` : "");
    const ok = window.confirm(
      `Force reprocess ${participants.length} folder(s)?\n\n${shown}\n\n` +
      "Everything is redone, including items already complete. Actigraph recordings " +
      "are re-downloaded from OneDrive (~1 GB each) and re-epoched, which can take a long time.");
    if (!ok) return;
  }
  startRun({ dryRun: mode === "preview", force: mode === "force", mode });
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
let currentRunMode = "new";   // mode of the job in progress, kept on its status pill

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

async function startRun({ dryRun = false, force = false, mode = "new" } = {}) {
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
  const tag = RUN_MODES[mode].tag;
  currentRunMode = mode;
  setJobStatus(tag, `running mode-${mode}`);
  logLine(`${tag}: ${participants.join(", ")}`, dry_run ? "warning" : force ? "warning" : "ok");
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
    body: JSON.stringify({ participants, force, dry_run, initials: currentInitials() }),
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
  setJobStatus(RUN_MODES[currentRunMode].tag, `running mode-${currentRunMode}`);
  const ws = new WebSocket(`ws://${location.host}/ws/jobs/${jobId}`);
  ws.onmessage = (ev) => handleEvent(JSON.parse(ev.data));
  ws.onclose = () => { if (!dryRun) realJobActive = false; };
  ws.onerror = () => { logLine("WebSocket error", "error"); if (!dryRun) realJobActive = false; };
}

function handleEvent(e) {
  if (e.type === "batch_start") logLine(`Batch ${e.batch_id} started by ${e.initials}`, "ok");
  if (e.type === "batch_end") { logLine(`Batch ${e.batch_id} recorded`, "ok"); loadBatches(); }
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
      setJobStatus(`${RUN_MODES[currentRunMode].tag} · ${e.status}`,
                   `${e.status === "done" ? "done" : "error"} mode-${currentRunMode}`);
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


// ------------------------------------------------------------------ batches (stage 1)
// Public records of each real run. ✕ archives (hides) a batch; nothing is deleted.
$("#initials").addEventListener("input", (ev) => {
  ev.target.value = ev.target.value.replace(/[^A-Za-z]/g, "").toUpperCase();
  ev.target.classList.remove("need");
});
$("#show-archived").addEventListener("change", loadBatches);

async function loadBatches() {
  const box = $("#batchlist");
  const data = await api(`/api/batches?stage=PRE&archived=${$("#show-archived").checked ? 1 : 0}`).catch(() => null);
  const list = (data && data.batches) || [];
  box.innerHTML = "";
  if (!list.length) { box.appendChild(el("div", "muted", "No batches yet.")); return; }
  list.forEach((b) => {
    const it = b.items || {};
    const row = el("div", `batch ${b.status} ${b.archived_flag ? "archived" : ""}`);
    const when = (b.started_at || "").replace("T", " ").slice(0, 16);
    row.appendChild(el("div", "b-main",
      `<span class="b-id">${esc(b.batch_id)}</span>` +
      `<span class="pill b-status ${b.status === "done" ? "done" : b.status === "cancelled" ? "" : "error"}">${esc(b.status)}</span>`));
    row.appendChild(el("div", "b-meta",
      `${esc(b.mode_label || b.mode)} · ${esc(b.initials)} · ${when} · ${b.n_participants} participant(s) · ` +
      `processed ${it.done || 0} · skipped ${it.skipped || 0}` +
      (it.failed ? ` · <span class="b-fail">failed ${it.failed}</span>` : "") +
      (b.archived ? ` · archived by ${esc(b.archived.by)}` : "")));
    if (!b.archived_flag) {
      const x = el("button", "b-x", "✕");
      x.title = "Archive: hide from the list (the record is kept)";
      x.addEventListener("click", async () => {
        const ini = currentInitials();
        if (!ini) { logLine("Enter your initials to archive a batch.", "warning"); $("#initials").focus(); return; }
        if (!window.confirm(`Archive batch ${b.batch_id}?
It will be hidden from this list; the record is kept.`)) return;
        const r = await api(`/api/batches/${encodeURIComponent(b.batch_id)}/archive`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ initials: ini }),
        }).catch(() => ({ error: "request failed" }));
        if (r.error) logLine(`Could not archive ${b.batch_id}: ${r.error}`, "error");
        loadBatches();
      });
      row.appendChild(x);
    }
    box.appendChild(row);
  });
}
loadBatches();


// ================================================================== T2
// Visualise -> T2 hand-off (records the selection, writes no data) and the T2
// preview, which reads the processed outputs on demand.
//
// Seasons are the CALENDAR season the recordings fall in: one person's 2nd
// season can be another's 3rd, and the folder labels disagree, so comparisons
// line up on "Autumn 2025", not on "s2".
const T2_DEVICES = [["actigraph", "Actigraph"], ["mieye", "MiEYE"], ["expiwell", "Expiwell"]];
const T2_DEVICE_COLOUR = { actigraph: "#4f8cff", mieye: "#f5b82e", expiwell: "#b58cff" };
const T2_LAGS = [["same", "same day"], ["night", "that night"], ["next", "next day"]];
const T2_VIEWS = [["coverage", "Coverage"], ["overlap", "Agreement"],
                  ["cross", "Relationships"], ["compare", "Compare"]];
// One plain sentence per view. Each view answers a different question.
const T2_VIEW_HELP = {
  coverage: "What data exists: the days each device recorded, and the days they overlap.",
  overlap: "Do two measures in the same unit agree, day by day? (e.g. is the darkest 2 h the same hour as the least-active 5 h)",
  cross: "Does one measure move with another, when the two are on different scales?",
  compare: "One measure, compared across your groups, the seasons of the year, or people.",
};
//: who each view compares, shown beside the question it answers
const T2_VIEW_SCOPE = {
  coverage: "one participant-season per row · every device",
  overlap: "one participant at a time · two measures sharing a unit",
  cross: "all participants pooled, split into within-person and between-person",
  compare: "all participants · one measure · grouped by season, group or person",
};
let T2_DICT = null;
const t2Modal = { participants: [], summary: null, checked: false };

async function t2Dictionary() {
  if (!T2_DICT) T2_DICT = await api("/api/t2/dictionary");
  return T2_DICT;
}
const postJSON = (url, body) => api(url, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});
function chip(name, value, label, checked, title = "", type = "checkbox") {
  return `<label class="seg" title="${esc(title)}"><input type="${type}" name="${name}" value="${esc(value)}" ${checked ? "checked" : ""}/><span>${label}</span></label>`;
}
const checkedValues = (name) => $$(`input[name=${name}]:checked`).map((i) => i.value);
const DAY_MS = 864e5;
const isoDay = (t) => new Date(t).toISOString().slice(0, 10);
function fmtNum(v) {
  if (v == null || Number.isNaN(v)) return "—";
  const a = Math.abs(v);
  return a >= 100 ? v.toFixed(0) : a >= 10 ? v.toFixed(1) : v.toFixed(2);
}

// ------------------------------------------------------------------ send dialog
$("#t2-send-btn").addEventListener("click", openT2Modal);
$("#t2m-cancel").addEventListener("click", () => $("#t2-modal").classList.add("hidden"));
$("#t2-modal").addEventListener("click", (e) => { if (e.target.id === "t2-modal") $("#t2-modal").classList.add("hidden"); });
$("#t2m-check-btn").addEventListener("click", runT2Check);
$("#t2m-send").addEventListener("click", sendT2);
$("#t2m-initials").addEventListener("input", (ev) => {
  ev.target.value = ev.target.value.replace(/[^A-Za-z]/g, "").toUpperCase();
  updateT2SendState();
});

async function openT2Modal() {
  const participants = viewGrid.list();
  if (!participants.length) {
    showToast("t2sel", "Select processed folders in Visualise first", 100);
    setTimeout(() => hideToast("t2sel"), 1800);
    return;
  }
  Object.assign(t2Modal, { participants, summary: null, checked: false });
  $("#t2m-sub").innerHTML = `<b>${participants.length}</b> participant folder(s). Choose what T2 should show, check the devices line up, then send. Nothing is copied or changed.`;
  $("#t2m-result").innerHTML = "";
  $("#t2m-note").textContent = "";
  $("#t2m-varlist").innerHTML = "";
  $("#t2m-name").value = "";
  $("#t2m-initials").value = $("#initials").value || "";
  $("#t2m-devices").innerHTML = T2_DEVICES.map(([k, l]) => chip("t2dev", k, l, true)).join("");
  $("#t2m-seasons").innerHTML = `<span class="muted">reading seasons…</span>`;
  renderT2Checks(null);                      // the checks are visible from the start
  $("#t2-modal").classList.remove("hidden");
  updateT2SendState();

  const [dict, seasons] = await Promise.all([t2Dictionary(), postJSON("/api/t2/seasons", { participants })]);
  const list = seasons.seasons || [];
  $("#t2m-seasons").innerHTML = list.length
    ? list.map((s) => chip("t2season", s.key,
        `${esc(s.label)} <em>${s.n_participants}</em>${s.estimated ? " •" : ""}`, true,
        `${s.label}: ${s.n_participants} of the selected participants have data in this season of the year.` +
        (s.estimated ? ` ${s.estimated} worked out from filenames rather than processed data.` : "") +
        ` Folders: ${s.folders.join(", ")}`)).join("")
    : `<span class="muted">No seasons found for these participants.</span>`;
  renderT2VarList(dict);
  $$("input[name=t2dev]").forEach((i) => i.addEventListener("change", () => { renderT2VarList(dict); invalidateT2Check(); }));
  $$("input[name=t2season]").forEach((i) => i.addEventListener("change", invalidateT2Check));
  updateT2SendState();
}

function renderT2VarList(dict) {
  const devices = checkedValues("t2dev");
  const host = $("#t2m-varlist");
  const firstRender = !host.children.length;
  const prev = new Set(checkedValues("t2var"));
  const shown = new Set($$("input[name=t2var]").map((i) => i.dataset.dev));
  host.innerHTML = "";
  T2_DEVICES.filter(([k]) => devices.includes(k)).forEach(([k, label]) => {
    const vars = dict.variables.filter((v) => v.device === k);
    const blk = el("div", "t2m-varblk");
    blk.appendChild(el("div", "t2m-varhead", `<b>${label}</b> <button class="btn ghost tiny" data-all="${k}">all</button><button class="btn ghost tiny" data-none="${k}">none</button>`));
    const grid = el("div", "t2m-vargrid");
    vars.forEach((v) => {
      const on = firstRender || !shown.has(k) || prev.has(v.id);
      grid.insertAdjacentHTML("beforeend",
        `<label class="t2m-var" title="${esc(v.definition)}"><input type="checkbox" name="t2var" value="${v.id}" data-dev="${k}" ${on ? "checked" : ""}/> ${esc(v.id)} <span class="muted">${esc(v.unit || "")}${v.level === "season" ? " · season" : ""}</span></label>`);
    });
    blk.appendChild(grid);
    host.appendChild(blk);
  });
  $$("[data-all]", host).forEach((b) => b.addEventListener("click", (e) => {
    e.preventDefault();
    $$(`input[data-dev=${b.dataset.all}]`, host).forEach((i) => { i.checked = true; });
    varsChanged();
  }));
  $$("[data-none]", host).forEach((b) => b.addEventListener("click", (e) => {
    e.preventDefault();
    $$(`input[data-dev=${b.dataset.none}]`, host).forEach((i) => { i.checked = false; });
    varsChanged();
  }));
  $$("input[name=t2var]", host).forEach((i) => i.addEventListener("change", () => varsChanged()));
  varsChanged(false);
}
function varsChanged(invalidate = true) {
  $("#t2m-varcount").textContent = `${checkedValues("t2var").length} of ${$$("input[name=t2var]").length} selected`;
  if (invalidate) invalidateT2Check();
}
function invalidateT2Check() {
  if (!t2Modal.checked) return;
  t2Modal.checked = false;
  t2Modal.summary = null;
  $("#t2m-result").innerHTML = `<div class="muted">Selection changed — check availability again.</div>`;
  renderT2Checks(null);
  updateT2SendState();
}
function t2Payload() {
  return {
    participants: t2Modal.participants,
    devices: checkedValues("t2dev"),
    seasons: checkedValues("t2season"),
    variables: checkedValues("t2var"),
    name: $("#t2m-name").value.trim(),
  };
}

// The checks the user must confirm - shown from the moment the dialog opens, so
// it is obvious what is still needed before SEND can do anything.
function renderT2Checks(summary) {
  const host = $("#t2m-confirm");
  const ticked = new Set(checkedValues("t2check"));
  const checks = summary ? summary.checks : { actigraph_60s: "The Actigraph 60 s outputs exist for this selection",
                                              verdicts_reviewed: "I have reviewed the compliance verdicts for these participants" };
  const missing60 = summary ? summary.flags.filter((f) => f.flag === "actigraph-60s-missing").length : null;
  host.innerHTML = `<div class="t2m-clab">Before you send — tick both</div>`;
  Object.entries(checks).forEach(([key, label]) => {
    let hint = `<span class="hint muted">run the availability check to confirm</span>`;
    if (key !== "actigraph_60s") hint = "";
    else if (summary) {
      hint = missing60
        ? `<span class="hint bad">✗ missing for ${missing60} participant-season(s)</span>`
        : `<span class="hint ok">✓ found</span>`;
    }
    host.insertAdjacentHTML("beforeend",
      `<label class="t2m-ack"><input type="checkbox" name="t2check" value="${key}" ${ticked.has(key) ? "checked" : ""}/> <span>${esc(label)}</span> ${hint}</label>`);
  });
  $$("input[name=t2check]", host).forEach((i) => i.addEventListener("change", updateT2SendState));
}

async function runT2Check() {
  const body = t2Payload();
  if (!body.devices.length || !body.seasons.length || !body.variables.length) {
    $("#t2m-result").innerHTML = `<div class="agg-note bad">Choose at least one season, device and variable.</div>`;
    return;
  }
  $("#t2m-result").innerHTML = `<div class="muted">checking ${body.participants.length} participant(s)…</div>`;
  const r = await postJSON("/api/t2/availability", body).catch(() => ({ error: "request failed" }));
  if (r.error) { $("#t2m-result").innerHTML = `<div class="agg-note bad">${esc(r.error)}</div>`; return; }
  t2Modal.summary = r;
  t2Modal.checked = true;
  $("#t2m-result").innerHTML = "";
  $("#t2m-result").appendChild(renderAvailability(r, { compact: true, devices: body.devices }));
  renderT2Checks(r);
  updateT2SendState();
}

// The send button is greyed until everything is done, and the line beneath it
// spells out exactly what is still missing.
function updateT2SendState() {
  const r = t2Modal.summary;
  const ticked = checkedValues("t2check");
  const needed = r ? Object.keys(r.checks) : ["actigraph_60s", "verdicts_reviewed"];
  const missingChecks = needed.filter((k) => !ticked.includes(k));
  const initialsOk = /^[A-Z]{2,4}$/.test($("#t2m-initials").value);
  const ready = t2Modal.checked && !missingChecks.length && initialsOk;
  const btn = $("#t2m-send");
  btn.disabled = !ready;
  btn.classList.toggle("ready", ready);
  btn.textContent = r && r.flag_counts.missing && ready ? "Send anyway" : "Send to T2";
  const todo = [];
  if (!t2Modal.checked) todo.push("check availability");
  if (missingChecks.length) todo.push(`tick ${missingChecks.length} box${missingChecks.length > 1 ? "es" : ""}`);
  if (!initialsOk) todo.push("enter your initials");
  $("#t2m-todo").innerHTML = todo.length
    ? `<span class="todo-x">Still to do:</span> ${todo.join(" · ")}`
    : `<span class="todo-ok">Ready to send.</span>`;
}

async function sendT2() {
  const btn = $("#t2m-send");
  btn.disabled = true;
  const body = {
    ...t2Payload(),
    initials: $("#t2m-initials").value,
    checks: Object.fromEntries(checkedValues("t2check").map((k) => [k, true])),
  };
  const r = await postJSON("/api/t2/selections", body).catch(() => ({ error: "request failed" }));
  if (r.error) { $("#t2m-note").textContent = r.error; btn.disabled = false; return; }
  $("#initials").value = body.initials;
  $("#t2-modal").classList.add("hidden");
  t2State.pending = r.selection.batch_id;
  $$(".tab").find((t) => t.dataset.tab === "t2").click();
}

// ------------------------------------------------------------------ availability
function renderAvailability(r, { compact = false, devices = null, onRow = null } = {}) {
  const box = el("div", "t2-avail");
  const miss = r.flag_counts.missing;
  const stats = el("div", "stat-row");
  stats.appendChild(el("div", "stat", `<div class="n">${r.selection.n_participants}</div><div class="k">participants</div>`));
  stats.appendChild(el("div", "stat", `<div class="n">${r.windows.length}</div><div class="k">participant-seasons</div>`));
  stats.appendChild(el("div", "stat", `<div class="n">${r.paired_days}</div><div class="k">days all devices overlap</div>`));
  stats.appendChild(el("div", `stat ${miss ? "bad" : ""}`, `<div class="n">${miss}</div><div class="k">gaps in the data</div>`));
  box.appendChild(stats);
  box.appendChild(el("div", miss ? "agg-note bad" : "agg-note ok", miss
    ? `${miss} gap(s): a device or measure with no data here, so it is simply absent from the comparisons.`
    : "Every selected device has data for each participant-season."));
  box.appendChild(renderCoverage(r, devices || r.selection.devices, onRow));
  if (r.flags.length) {
    const list = el("div", `flaglist ${compact ? "compact" : ""}`);
    const order = { missing: 0, info: 1 };
    [...r.flags]
      .sort((a, b) => (order[a.severity] - order[b.severity]) || String(a.display_id).localeCompare(String(b.display_id)))
      .forEach((f) => {
        const sev = f.severity === "missing" ? "skipped" : "info";
        const where = [f.display_id, f.season_label || (f.season_n ? `s${f.season_n}` : ""), f.device || "",
                       f.source || "", f.column ? `column ${f.column}` : ""].filter(Boolean).join(" · ");
        list.appendChild(el("div", `flag ${sev}`,
          `<div class="flag-top"><span class="fsev ${sev}">${sev === "skipped" ? "✕ missing" : "i info"}</span><span class="fwhere">${esc(where)}</span></div><div class="fmsg">${esc(f.text)}</div>`));
      });
    box.appendChild(el("h3", "sub", `What is missing or worth knowing`));
    box.appendChild(list);
  }
  return box;
}

function renderCoverage(r, devices, onRow) {
  const wrap = el("div", "t2-cov");
  if (!r.windows.length) { wrap.appendChild(el("div", "muted", "No processed day-level data for this selection.")); return wrap; }
  const tbl = el("table", "tbl cov");
  tbl.innerHTML = `<tr><th>participant</th><th>season</th><th>days each device recorded · shaded = all overlap</th><th>overlap</th></tr>`;
  r.windows.forEach((w) => {
    const spans = w.device_spans || {};
    const all = Object.values(spans).flat().map((d) => Date.parse(d));
    const lo = Math.min(...all), hi = Math.max(...all);
    const span = Math.max(1, (hi - lo) / DAY_MS + 1);
    const pos = (d) => ((Date.parse(d) - lo) / DAY_MS) / span * 100;
    const wid = (a, b) => ((Date.parse(b) - Date.parse(a)) / DAY_MS + 1) / span * 100;
    let bars = "";
    if (w.start) bars += `<div class="cov-win" style="left:${pos(w.start)}%;width:${wid(w.start, w.end)}%"></div>`;
    devices.forEach((d, i) => {
      const s = spans[d];
      bars += s
        ? `<div class="cov-bar" title="${d}: ${s[0]} → ${s[1]}" style="top:${5 + i * 11}px;left:${pos(s[0])}%;width:${wid(s[0], s[1])}%;background:${T2_DEVICE_COLOUR[d]}"></div>`
        : `<div class="cov-none" style="top:${5 + i * 11}px">no ${d}</div>`;
    });
    const tr = el("tr", onRow ? "clickable" : "");
    tr.innerHTML = `<td class="mono">${esc(w.display_id)}</td>` +
      `<td><b>${esc(w.season_label || w.season)}</b><div class="muted small">folder: ${esc(w.season)} · s${w.season_n}</div></td>` +
      `<td><div class="cov-track" style="height:${10 + devices.length * 11}px">${bars}</div><div class="cov-dates muted">${all.length ? `${isoDay(lo)} → ${isoDay(hi)}` : ""}</div></td>` +
      `<td class="${w.days ? (w.days < 7 ? "warnc" : "") : "badc"}">${w.days ? `${w.days} days` : "none"}${w.start ? `<div class="muted small">${w.start} → ${w.end}</div>` : ""}</td>`;
    if (onRow) tr.addEventListener("click", () => onRow(w, tr));
    tbl.appendChild(tr);
  });
  wrap.appendChild(tbl);
  wrap.appendChild(el("div", "legend",
    devices.map((d) => `<span><i class="sw" style="background:${T2_DEVICE_COLOUR[d]}"></i>${d}</span>`).join("") +
    `<span><i class="sw cov-sw"></i>all devices overlap</span>`));
  return wrap;
}

// ------------------------------------------------------------------ T2 panel
const t2State = { id: null, pending: null, summary: null, record: null, days: [1, 1], maxDays: 1,
                  timeline: null, vars: null, view: "coverage", groups: null,
                  excludePartial: true, cutoffs: [], exportPreview: null };
$("#t2-show-archived").addEventListener("change", loadT2Selections);

async function loadT2Selections() {
  const box = $("#t2-sellist");
  const data = await api(`/api/t2/selections?archived=${$("#t2-show-archived").checked ? 1 : 0}`).catch(() => null);
  const list = (data && data.selections) || [];
  box.innerHTML = "";
  if (!list.length) {
    box.innerHTML = `<div class="muted">Nothing sent yet. Select folders in <b>Visualise</b> and press <b>SEND TO T2</b>.</div>`;
  }
  list.forEach((b) => {
    const row = el("div", `batch t2sel ${b.archived_flag ? "archived" : ""} ${b.batch_id === t2State.id ? "active" : ""}`);
    row.dataset.id = b.batch_id;
    const when = (b.started_at || "").replace("T", " ").slice(0, 16);
    row.appendChild(el("div", "b-main", `<span class="b-id">${esc(b.batch_id)}</span>`));
    row.appendChild(el("div", "b-meta",
      `${b.name ? `<b>${esc(b.name)}</b> · ` : ""}${esc(b.initials)} · ${when}<br>` +
      `${b.n_participants} participant(s) · ${(b.devices || []).join(", ")}` +
      ((b.exports || []).length
        ? `<br><span class="sel-exports">✓ exported ${b.exports.length}× · last ${esc((b.exports[b.exports.length - 1].at || "").replace("T", " ").slice(0, 16))}</span>`
        : "") +
      (b.archived ? ` · archived by ${esc(b.archived.by)}` : "")));
    row.addEventListener("click", () => openT2Selection(b.batch_id));
    if (!b.archived_flag) {
      const x = el("button", "b-x", "✕");
      x.title = "Archive: hide from the list (the record is kept)";
      x.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const ini = currentInitials();
        if (!ini) {
          showToast("t2x", "Enter your initials on the Compliance tab to archive", 100);
          setTimeout(() => hideToast("t2x"), 2200);
          return;
        }
        if (!window.confirm(`Archive selection ${b.batch_id}?\nIt will be hidden from this list; the record is kept.`)) return;
        const r = await postJSON(`/api/t2/selections/${encodeURIComponent(b.batch_id)}/archive`, { initials: ini })
          .catch(() => ({ error: "request failed" }));
        if (r.error) window.alert(`Could not archive: ${r.error}`);
        if (t2State.id === b.batch_id) {
          t2State.id = null;
          $("#t2-title").textContent = "T2 preview";
          $("#t2-body").innerHTML = `<div class="muted">Pick a selection on the left.</div>`;
        }
        loadT2Selections();
      });
      row.appendChild(x);
    }
    box.appendChild(row);
  });
  const privacy = await api("/api/privacy").catch(() => null);
  if (privacy) {
    $("#t2-privacy").textContent = privacy.enabled ? "pseudonymous IDs" : "real IDs · pseudonymisation off";
    $("#t2-privacy").className = `pill ${privacy.enabled ? "done" : ""}`;
  }
  if (t2State.pending) {
    const id = t2State.pending;
    t2State.pending = null;
    openT2Selection(id);
  }
}

async function openT2Selection(id) {
  t2State.id = id;
  t2State.timeline = null;
  t2State.vars = null;
  t2State.groups = null;
  t2State.cutoffs = [];
  t2State.exportPreview = null;
  t2cut.info = t2cut.ranges = t2cut.draft = null;
  t2x.x = t2x.y = t2x.matrixVars = null;
  t2cmp.variable = null;
  $$(".t2sel").forEach((r) => r.classList.toggle("active", r.dataset.id === id));
  $("#t2-title").textContent = `T2 preview · ${id}`;
  $("#t2-body").innerHTML = `<div class="muted">reading outputs…</div>`;
  const [r] = await Promise.all([
    api(`/api/t2/selections/${encodeURIComponent(id)}`).catch(() => ({ error: "request failed" })),
    t2Dictionary(),
  ]);
  if (t2State.id !== id) return;
  if (r.error) { $("#t2-body").innerHTML = `<div class="agg-note bad">${esc(r.error)}</div>`; return; }
  t2State.record = r.record;
  t2State.summary = r.summary;
  t2State.sameUnit = r.same_unit || {};
  t2State.people = (r.summary.windows || []).map((w) => w.display_id)
    .filter((v, i, all) => all.indexOf(v) === i);
  const maxDays = Math.max(1, ...r.summary.windows.map((w) => w.days || 0));
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(`t2days:${id}`) || "null"); } catch { saved = null; }
  t2State.maxDays = maxDays;
  t2State.days = Array.isArray(saved) && saved[0] >= 1 && saved[1] <= maxDays && saved[0] <= saved[1] ? saved : [1, maxDays];
  renderT2Body();
}

function renderT2Body() {
  const { record: rec, summary: sum } = t2State;
  const body = $("#t2-body");
  body.innerHTML = "";
  body.appendChild(el("div", "t2-head",
    `${rec.name ? `<b>${esc(rec.name)}</b> · ` : ""}sent by ${esc(rec.initials)} · ${(rec.started_at || "").replace("T", " ").slice(0, 16)} · ` +
    `${(rec.devices || []).join(", ")} · ${(rec.variables || []).length} measures · ` +
    `${sum.windows.length} participant-seasons`));

  // Day range within each participant-season's shared window (day 1 = its first
  // shared day). Day-by-day, with week shortcuts.
  const sl = el("div", "t2-days");
  sl.innerHTML =
    `<span class="t2m-lbl">Days</span>` +
    `<input type="range" id="t2-d-from" min="1" max="${t2State.maxDays}" value="${t2State.days[0]}" title="first day">` +
    `<input type="range" id="t2-d-to" min="1" max="${t2State.maxDays}" value="${t2State.days[1]}" title="last day">` +
    `<span id="t2-d-text"></span>` +
    `<span class="t2-quick">${dayButtons().map((b) =>
      `<button class="btn ghost tiny" data-from="${b.from}" data-to="${b.to}">${b.label}</button>`).join("")}</span>` +
    `<span class="t2-filters">` +
      `<label class="chk" title="The first and last day of a recording are usually only half recorded"><input type="checkbox" id="t2-partial" ${t2State.excludePartial ? "checked" : ""}/> drop part-days</label>` +
    `</span>`;
  body.appendChild(sl);
  const apply = () => {
    $("#t2-d-from").value = t2State.days[0];
    $("#t2-d-to").value = t2State.days[1];
    try { localStorage.setItem(`t2days:${t2State.id}`, JSON.stringify(t2State.days)); } catch { /* per-viewer only */ }
    renderDayText();
    clearTimeout(t2State._timer);
    t2State._timer = setTimeout(() => {
      if (t2State.view === "coverage") { if (t2State.timeline) renderTimeline(t2State.timeline); }
      else renderT2View(t2State.view);
    }, 250);
  };
  const onDay = (which) => (ev) => {
    let [a, b] = t2State.days;
    const v = Number(ev.target.value);
    if (which === 0) { a = v; if (a > b) b = a; } else { b = v; if (b < a) a = b; }
    t2State.days = [a, b];
    apply();
  };
  $("#t2-d-from", sl).addEventListener("input", onDay(0));
  $("#t2-d-to", sl).addEventListener("input", onDay(1));
  $("#t2-partial", sl).addEventListener("change", (ev) => {
    t2State.excludePartial = ev.target.checked;
    renderT2View(t2State.view);
  });
  $$("[data-from]", sl).forEach((b) => b.addEventListener("click", () => {
    t2State.days = [Number(b.dataset.from), Number(b.dataset.to)];
    apply();
  }));
  renderDayText();

  renderCutoffBar(body);

  const tabs = el("div", "t2-views");
  T2_VIEWS.forEach(([key, label]) => {
    const b = el("button", `btn ghost t2-view ${t2State.view === key ? "active" : ""}`, label);
    b.addEventListener("click", () => {
      t2State.view = key;
      $$(".t2-view").forEach((x) => x.classList.toggle("active", x === b));
      $(".t2-help").innerHTML = `${T2_VIEW_HELP[key]} <span class="t2-scope">${T2_VIEW_SCOPE[key]}</span>`;
      renderT2View(key);
    });
    tabs.appendChild(b);
  });
  body.appendChild(tabs);
  body.appendChild(el("div", "t2-help muted",
    `${T2_VIEW_HELP[t2State.view]} <span class="t2-scope">${T2_VIEW_SCOPE[t2State.view]}</span>`));
  body.appendChild(el("div", "t2-viewbody"));
  renderT2View(t2State.view);
}

// Buttons say which days they cover. A 15-day window gives "days 1-7",
// "days 8-15" - never a one-day "week 3".
function dayButtons() {
  const max = t2State.maxDays;
  const out = [{ from: 1, to: max, label: `all ${max} days` }];
  for (let start = 1; start + 6 <= max; start += 7) {
    const remainder = max - (start + 6);
    const end = remainder > 0 && remainder < 4 ? max : Math.min(start + 6, max);
    out.push({ from: start, to: end, label: `days ${start}\u2013${end}` });
    if (end === max) break;
  }
  return out.length > 1 ? out : [];
}

$("#t2-export").addEventListener("click", openExport);
$("#t2x-cancel").addEventListener("click", () => $("#t2x-modal").classList.add("hidden"));
$("#t2x-modal").addEventListener("click", (e) => { if (e.target.id === "t2x-modal") $("#t2x-modal").classList.add("hidden"); });
$("#t2x-go").addEventListener("click", runExport);
$("#t2x-initials").addEventListener("input", (ev) => {
  ev.target.value = ev.target.value.replace(/[^A-Za-z]/g, "").toUpperCase();
  updateExportState();
});

function renderDayText() {
  const [a, b] = t2State.days;
  const whole = a === 1 && b === t2State.maxDays;
  $("#t2-d-text").innerHTML = whole
    ? `all ${t2State.maxDays} days of each shared window`
    : `day ${a}${b > a ? `–${b}` : ""} of each shared window <span class="muted">(${b - a + 1} day${b - a ? "s" : ""})</span>`;
}

// Everything here already passed the Visualise panel, so T2 does not filter on
// compliance again: every recorded day is included. Part-days are about
// coverage, not compliance, so that one stays.
function t2Filters() {
  return { days: t2State.days, valid_only: false, exclude_partial: t2State.excludePartial,
           cutoffs: t2State.cutoffs || [] };
}

function filterWords() {
  return t2State.excludePartial ? "part-days dropped" : "every day, part-days included";
}

function renderT2View(which) {
  const host = $(".t2-viewbody");
  if (!host) return;
  host.innerHTML = "";
  if (which === "cross") { renderCrossDevice(host); return; }
  if (which === "compare") { renderCompare(host); return; }
  if (which === "overlap") { renderOverlap(host); return; }
  host.appendChild(renderAvailability(t2State.summary, {
    devices: (t2State.record || {}).devices,
    onRow: (w, tr) => {
      $$(".tbl.cov tr", host).forEach((x) => x.classList.remove("active"));
      tr.classList.add("active");
      loadTimeline(w);
    },
  }));
  host.appendChild(el("div", "t2-timeline", `<div class="muted">Click a row above to see that person's days.</div>`));
  const rows = $$(".tbl.cov tr.clickable", host);
  const firstIdx = t2State.summary.windows.findIndex((w) => w.days);
  if (firstIdx >= 0 && rows[firstIdx]) rows[firstIdx].click();
}

// ------------------------------------------------------------------ timelines
const T2_DEFAULT_VARS = ["act_wear_hours", "act_l5_hour", "light_mel_mean", "light_l2_onset",
                         "diary_tst", "diary_se", "affect_positive"];
const T2_MAX_CHARTS = 8;

async function loadTimeline(w) {
  const host = $(".t2-timeline");
  host.innerHTML = `<div class="muted">reading ${esc(w.display_id)}…</div>`;
  const id = t2State.id;
  const r = await postJSON("/api/t2/timeline", { selection: id, display_id: w.display_id, season_n: w.season_n })
    .catch(() => ({ error: "request failed" }));
  if (t2State.id !== id) return;
  if (r.error) { host.innerHTML = `<div class="agg-note bad">${esc(r.error)}</div>`; return; }
  t2State.timeline = r;
  t2State.timelineSeason = w.season_label || w.season;
  renderTimeline(r);
}

function renderTimeline(r) {
  const host = $(".t2-timeline");
  if (!host) return;
  host.innerHTML = "";
  host.appendChild(el("h3", "sub",
    `${esc(r.display_id)} · ${esc(t2State.timelineSeason || "")}` +
    (r.window && r.window.start ? ` · all devices overlap ${r.window.start} → ${r.window.end}` : " · no overlap")));
  if (!r.series.length) { host.appendChild(el("div", "muted", "No day-level values here.")); return; }

  if (!t2State.vars) {
    const preferred = T2_DEFAULT_VARS.filter((v) => r.series.some((s) => s.variable === v));
    t2State.vars = new Set((preferred.length ? preferred : r.series.map((s) => s.variable)).slice(0, T2_MAX_CHARTS));
  }
  host.appendChild(renderVarPicker(r));
  const series = r.series.filter((s) => t2State.vars.has(s.variable));
  if (!series.length) { host.appendChild(el("div", "muted", "Pick at least one measure above.")); return; }

  const dates = series.flatMap((s) => s.points.map((p) => Date.parse(p[0])));
  const lo = Math.min(...dates), hi = Math.max(...dates);
  const W = 1000, H = 58, GAP = 6, L = 170, R = 56, TOP = 16;
  const span = Math.max(DAY_MS, hi - lo);
  const x = (t) => L + ((t - lo) / span) * (W - L - R);
  const xd = (d) => x(Date.parse(d));
  const plotH = series.length * (H + GAP);
  const [da, db] = t2State.days;
  const inRange = (p) => p[4] != null && p[4] >= da && p[4] <= db;
  const parts = [];

  for (let t = lo; t <= hi; t += 7 * DAY_MS) {
    parts.push(`<line x1="${x(t)}" x2="${x(t)}" y1="${TOP}" y2="${TOP + plotH}" class="tl-grid"/><text x="${x(t)}" y="${TOP - 4}" class="tl-tick">${isoDay(t).slice(5)}</text>`);
  }
  if (r.window && r.window.start) {
    const ws = Date.parse(r.window.start), we = Date.parse(r.window.end);
    parts.push(`<rect x="${x(ws) - 3}" y="${TOP}" width="${Math.max(6, x(we) - x(ws) + 6)}" height="${plotH}" class="tl-win"/>`);
    const sa = ws + (da - 1) * DAY_MS, sb = Math.min(we, ws + (db - 1) * DAY_MS);
    if (sa <= we) parts.push(`<rect x="${x(sa) - 3}" y="${TOP}" width="${Math.max(6, x(sb) - x(sa) + 6)}" height="${plotH}" class="tl-sel"/>`);
  }

  let y0 = TOP;
  series.forEach((s) => {
    const pts = s.points.filter((p) => p[1] != null);
    const col = T2_DEVICE_COLOUR[s.device] || "#9aa";
    const night = s.timing === "diary_morning" ? " · night before" : s.timing !== "day" ? " · night of date" : "";
    parts.push(`<text x="4" y="${y0 + 20}" class="tl-name"><title>${esc(s.definition || "")}</title>${esc(s.variable)}</text>`);
    parts.push(`<text x="4" y="${y0 + 34}" class="tl-unit">${esc(s.unit || "")}${night}</text>`);
    if (pts.length) {
      const vals = pts.map((p) => p[1]);
      const vmin = Math.min(...vals), vmax = Math.max(...vals);
      const y = (v) => y0 + H - 8 - ((v - vmin) / ((vmax - vmin) || 1)) * (H - 16);
      parts.push(`<text x="${W - R + 6}" y="${y0 + 12}" class="tl-unit">${fmtNum(vmax)}</text><text x="${W - R + 6}" y="${y0 + H - 6}" class="tl-unit">${fmtNum(vmin)}</text>`);
      parts.push(`<polyline class="tl-line" stroke="${col}" points="${pts.map((p) => `${xd(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ")}"/>`);
      pts.forEach((p) => {
        const cls = `tl-pt${p[3] ? " partial" : ""}${inRange(p) ? "" : " dim"}`;
        const tip = `${p[0]}: ${fmtNum(p[1])} ${s.unit || ""}${p[3] ? " · part-day" : ""}${p[4] ? ` · day ${p[4]} of the shared window` : " · outside the shared window"}`;
        parts.push(`<circle class="${cls}" cx="${xd(p[0]).toFixed(1)}" cy="${y(p[1]).toFixed(1)}" r="3" fill="${col}" stroke="${col}"><title>${esc(tip)}</title></circle>`);
      });
    } else {
      parts.push(`<text x="${L}" y="${y0 + 30}" class="tl-unit">no values</text>`);
    }
    parts.push(`<line x1="0" x2="${W}" y1="${y0 + H + GAP / 2}" y2="${y0 + H + GAP / 2}" class="tl-sep"/>`);
    y0 += H + GAP;
  });

  host.appendChild(el("div", "legend",
    `<span><i class="sw tl-sw-valid"></i>a day</span><span><i class="sw tl-sw-partial"></i>part-day</span>` +
    `<span><i class="sw cov-sw"></i>all devices overlap</span><span><i class="sw tl-sw-sel"></i>chosen days</span>`));
  host.appendChild(el("div", "tl-wrap", `<svg class="tl-svg" viewBox="0 0 ${W} ${y0 + 4}" preserveAspectRatio="xMinYMin meet">${parts.join("")}</svg>`));

  if (r.season_values.length) {
    host.appendChild(el("h3", "sub", "Whole-season values"));
    const tbl = el("table", "tbl");
    tbl.innerHTML = `<tr><th>measure</th><th>device</th><th>value</th><th>unit</th></tr>` +
      r.season_values.map((v) => `<tr title="${esc(v.definition || "")}"><td class="mono">${esc(v.variable)}</td><td>${esc(v.device)}</td><td>${fmtNum(v.value)}</td><td>${esc(v.unit || "")}</td></tr>`).join("");
    host.appendChild(tbl);
  }
}

function renderVarPicker(r) {
  const box = el("div", "tl-pick");
  box.appendChild(el("span", "t2m-lbl", "Show"));
  const chips = el("div", "t2m-chips");
  T2_DEVICES.forEach(([dev]) => {
    r.series.filter((s) => s.device === dev).forEach((s) => {
      const on = t2State.vars.has(s.variable);
      const lab = el("label", `seg tl-chip ${on ? "on" : ""}`,
        `<input type="checkbox" ${on ? "checked" : ""}/><span>${esc(s.variable)}</span>`);
      lab.title = s.definition || "";
      $("input", lab).addEventListener("change", (ev) => {
        if (ev.target.checked) {
          if (t2State.vars.size >= T2_MAX_CHARTS) {
            ev.target.checked = false;
            showToast("t2vars", `Up to ${T2_MAX_CHARTS} charts at a time`, 100);
            setTimeout(() => hideToast("t2vars"), 1600);
            return;
          }
          t2State.vars.add(s.variable);
        } else {
          t2State.vars.delete(s.variable);
        }
        renderTimeline(t2State.timeline);
      });
      chips.appendChild(lab);
    });
  });
  box.appendChild(chips);
  box.appendChild(el("span", "muted small", `${t2State.vars.size} of ${r.series.length} · max ${T2_MAX_CHARTS}`));
  return box;
}

// ------------------------------------------------------------------ cross-device
// Two questions, never mixed: do people with more X have more Y (between), and
// on a person's own higher-X days is Y higher (within)?
const t2x = { x: null, y: null, lag: "same", centred: false,
              scatter: null, matrixVars: null, which: "within" };

function t2DayVariables() {
  const rec = t2State.record || {};
  const dict = (T2_DICT && T2_DICT.variables) || [];
  const chosen = new Set(rec.variables || dict.map((v) => v.id));
  return dict.filter((v) => v.level === "day" && chosen.has(v.id) && (rec.devices || []).includes(v.device));
}

function renderCrossDevice(host) {
  const vars = t2DayVariables();
  if (vars.length < 2) {
    host.appendChild(el("div", "muted", "This selection needs at least two day-level measures."));
    return;
  }
  const options = (chosen) => T2_DEVICES.map(([dev, label]) => {
    const opts = vars.filter((v) => v.device === dev)
      .map((v) => `<option value="${v.id}" ${v.id === chosen ? "selected" : ""}>${esc(v.id)}${v.unit ? ` (${esc(v.unit)})` : ""}</option>`).join("");
    return opts ? `<optgroup label="${label}">${opts}</optgroup>` : "";
  }).join("");

  if (!t2x.x || !vars.some((v) => v.id === t2x.x)) t2x.x = (vars.find((v) => v.device === "mieye") || vars[0]).id;
  if (!t2x.y || !vars.some((v) => v.id === t2x.y) || t2x.y === t2x.x) {
    const xDev = (vars.find((v) => v.id === t2x.x) || {}).device;
    t2x.y = (vars.find((v) => v.device !== xDev) || vars.find((v) => v.id !== t2x.x)).id;
  }

  const ctl = el("div", "xd-ctl");
  ctl.innerHTML =
    `<label class="small muted">x <select id="xd-x">${options(t2x.x)}</select></label>` +
    `<label class="small muted">y <select id="xd-y">${options(t2x.y)}</select></label>` +
    `<span class="t2m-chips" id="xd-lag">${T2_LAGS.map(([k, l]) =>
      `<label class="seg" title="${k === "night" ? "y is the night that starts on x's date" : k === "next" ? "y is the day after x" : "x and y on the same date"}"><input type="radio" name="xdlag" value="${k}" ${k === t2x.lag ? "checked" : ""}/><span>${l}</span></label>`).join("")}</span>` +
    `<span class="muted small">${filterWords()} — change above</span>`;
  host.appendChild(ctl);
  host.appendChild(el("div", "xd-body", `<div class="muted">pairing days…</div>`));
  host.appendChild(el("div", "xd-matrix"));

  const reload = (alsoMatrix) => {
    t2x.x = $("#xd-x").value;
    t2x.y = $("#xd-y").value;
    t2x.lag = ($("input[name=xdlag]:checked") || {}).value || "same";
    loadScatter();
    if (alsoMatrix) loadMatrix();
  };
  $("#xd-x", ctl).addEventListener("change", () => reload(false));
  $("#xd-y", ctl).addEventListener("change", () => reload(false));
  $$("input[name=xdlag]", ctl).forEach((i) => i.addEventListener("change", () => reload(true)));
  loadScatter();
  loadMatrix();
}

async function loadScatter() {
  const host = $(".xd-body");
  if (!host) return;
  host.innerHTML = `<div class="muted">pairing days…</div>`;
  const r = await postJSON("/api/t2/scatter", { selection: t2State.id, x: t2x.x, y: t2x.y, lag: t2x.lag, ...t2Filters() })
    .catch(() => ({ error: "request failed" }));
  if (r.error) { host.innerHTML = `<div class="agg-note bad">${esc(r.error)}</div>`; return; }
  t2x.scatter = r;
  renderScatter();
}

function renderScatter() {
  const r = t2x.scatter;
  const host = $(".xd-body");
  host.innerHTML = "";
  const s = r.stats;
  if (!r.points.length) {
    host.appendChild(el("div", "agg-note bad",
      `No days pair up for <b>${esc(r.x.id)}</b> and <b>${esc(r.y.id)}</b> (${esc(r.lag_label)}). Widen the day range, or include part-days.`));
    return;
  }
  const lagWord = r.lag === "same" ? "the same day" : r.lag === "night" ? "that night" : "the next day";
  host.appendChild(el("div", "xd-what",
    `Each dot is one day: <b>${esc(r.x.id)}</b>${r.x.unit ? ` (${esc(r.x.unit)})` : ""} across, ` +
    `<b>${esc(r.y.id)}</b>${r.y.unit ? ` (${esc(r.y.unit)})` : ""} up — taken ${lagWord}. ` +
    `${s.n_pairs} days from ${s.n_participants} people · ${filterWords()}.`));

  const card = (title, question, stat, n) => {
    const v = stat.r;
    const ci = stat.ci ? `95% CI ${stat.ci[0]} to ${stat.ci[1]}` : "no interval";
    const p = stat.p == null ? "" : ` · p ${stat.p < 0.001 ? "< 0.001" : stat.p.toFixed(3)}`;
    return `<div class="xd-stat"><div class="xd-slab">${title}<span class="muted">${question}</span></div>` +
      `<div class="xd-sval ${v == null ? "muted" : Math.abs(v) >= 0.3 ? "strong" : ""}">${v == null ? "—" : v.toFixed(2)}</div>` +
      `<div class="muted small">${ci}${p} · ${n}</div></div>`;
  };
  const box = el("div", "xd-stats");
  box.innerHTML =
    card("Between people", "do people with more x have more y?", s.between, `${s.between.n} people`) +
    card("Within a person", "on their own higher-x days, is y higher?", s.within, `${s.within.n_pairs} days`) +
    `<div class="xd-stat"><div class="xd-slab">Typical person<span class="muted">each person on their own</span></div>` +
    `<div class="xd-sval ${s.within.per_person_median_r == null ? "muted" : ""}">${s.within.per_person_median_r == null ? "—" : s.within.per_person_median_r.toFixed(2)}</div>` +
    `<div class="muted small">middle of ${s.within.n_persons_used} people</div></div>`;
  host.appendChild(box);
  if (s.note) host.appendChild(el("div", "agg-note bad", esc(s.note)));

  const toggle = el("label", "chk xd-centre",
    `<input type="checkbox" ${t2x.centred ? "checked" : ""}/> show each person's days around their own average`);
  $("input", toggle).addEventListener("change", (ev) => { t2x.centred = ev.target.checked; renderScatter(); });
  host.appendChild(toggle);
  host.appendChild(drawScatter(r));
}

function drawScatter(r) {
  const W = 700, H = 420, L = 68, B = 48, T = 12, R = 16;
  const ids = [...new Set(r.points.map((p) => p.display_id))];
  const colour = (id) => `hsl(${(ids.indexOf(id) * 47) % 360} 70% 62%)`;
  let pts = r.points.map((p) => ({ ...p }));
  const means = {};
  pts.forEach((p) => {
    const a = (means[p.display_id] = means[p.display_id] || { x: 0, y: 0, n: 0 });
    a.x += p.x; a.y += p.y; a.n += 1;
  });
  if (t2x.centred) {
    pts = pts.map((p) => ({ ...p, x: p.x - means[p.display_id].x / means[p.display_id].n,
                                  y: p.y - means[p.display_id].y / means[p.display_id].n }));
  }
  const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
  const pad = (a) => { const lo = Math.min(...a), hi = Math.max(...a); const m = (hi - lo || 1) * 0.06; return [lo - m, hi + m]; };
  const [x0, x1] = pad(xs), [y0, y1] = pad(ys);
  const px = (v) => L + ((v - x0) / (x1 - x0)) * (W - L - R);
  const py = (v) => H - B - ((v - y0) / (y1 - y0)) * (H - B - T);

  const parts = [`<rect x="${L}" y="${T}" width="${W - L - R}" height="${H - B - T}" class="sc-plot"/>`];
  for (let i = 0; i <= 4; i++) {
    const vx = x0 + (i / 4) * (x1 - x0), vy = y0 + (i / 4) * (y1 - y0);
    parts.push(`<line x1="${L}" x2="${W - R}" y1="${py(vy)}" y2="${py(vy)}" class="sc-grid"/>`);
    parts.push(`<text x="${L - 6}" y="${py(vy) + 3}" class="sc-lab" text-anchor="end">${fmtNum(vy)}</text>`);
    parts.push(`<line x1="${px(vx)}" x2="${px(vx)}" y1="${T}" y2="${H - B}" class="sc-grid"/>`);
    parts.push(`<text x="${px(vx)}" y="${H - B + 14}" class="sc-lab" text-anchor="middle">${fmtNum(vx)}</text>`);
  }
  const n = pts.length;
  const mx = xs.reduce((a, b) => a + b, 0) / n, my = ys.reduce((a, b) => a + b, 0) / n;
  let sxy = 0, sxx = 0;
  pts.forEach((p) => { sxy += (p.x - mx) * (p.y - my); sxx += (p.x - mx) ** 2; });
  if (sxx > 0) {
    const b1 = sxy / sxx, b0 = my - b1 * mx;
    parts.push(`<line x1="${px(x0)}" y1="${py(b0 + b1 * x0)}" x2="${px(x1)}" y2="${py(b0 + b1 * x1)}" class="sc-fit"/>`);
  }
  if (!t2x.centred) {
    Object.entries(means).forEach(([id, a]) => {
      parts.push(`<rect x="${(px(a.x / a.n) - 4).toFixed(1)}" y="${(py(a.y / a.n) - 4).toFixed(1)}" width="8" height="8" class="sc-mean" fill="${colour(id)}"><title>${esc(id)} average</title></rect>`);
    });
  }
  pts.forEach((p) => {
    parts.push(`<circle cx="${px(p.x).toFixed(1)}" cy="${py(p.y).toFixed(1)}" r="2.8" class="sc-pt" fill="${colour(p.display_id)}">` +
      `<title>${esc(p.display_id)} ${p.date}\n${esc(r.x.id)} ${fmtNum(p.x)}\n${esc(r.y.id)} ${fmtNum(p.y)}</title></circle>`);
  });
  parts.push(`<text x="${(L + W - R) / 2}" y="${H - 6}" class="sc-axis" text-anchor="middle">${esc(r.x.id)}${r.x.unit ? ` (${esc(r.x.unit)})` : ""}${t2x.centred ? " · around own average" : ""}</text>`);
  parts.push(`<text x="14" y="${(T + H - B) / 2}" class="sc-axis" transform="rotate(-90 14 ${(T + H - B) / 2})" text-anchor="middle">${esc(r.y.id)}${r.y.unit ? ` (${esc(r.y.unit)})` : ""}${t2x.centred ? " · around own average" : ""}</text>`);

  const wrap = el("div", "sc-wrap");
  wrap.innerHTML = `<svg class="sc-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMinYMin meet">${parts.join("")}</svg>`;
  wrap.appendChild(el("div", "legend sc-legend",
    ids.slice(0, 10).map((id) => `<span><i class="sw" style="background:${colour(id)};border-radius:50%"></i>${esc(id)}</span>`).join("") +
    (ids.length > 10 ? `<span class="muted">+${ids.length - 10} more</span>` : "") +
    (t2x.centred ? "" : `<span><i class="sw sc-sw-mean"></i>person's average</span>`)));
  return wrap;
}

async function loadMatrix() {
  const host = $(".xd-matrix");
  if (!host) return;
  const vars = t2DayVariables().map((v) => v.id);
  if (!t2x.matrixVars) {
    const queues = T2_DEVICES.map(([dev]) => t2DayVariables().filter((v) => v.device === dev).map((v) => v.id));
    const picked = [];
    for (let i = 0; picked.length < Math.min(9, vars.length) && i < 20; i++) {
      queues.forEach((q) => { if (q[i] && picked.length < 9) picked.push(q[i]); });
    }
    t2x.matrixVars = picked;
  }
  host.innerHTML = `<h3 class="sub">Every pair at a glance</h3><div class="muted">correlating…</div>`;
  const r = await postJSON("/api/t2/matrix", {
    selection: t2State.id, variables: t2x.matrixVars, lag: t2x.lag, which: t2x.which, ...t2Filters(),
  }).catch(() => ({ error: "request failed" }));
  host.innerHTML = "";
  host.appendChild(el("h3", "sub", "Every pair at a glance"));
  if (r.error) { host.appendChild(el("div", "agg-note bad", esc(r.error))); return; }

  const ctl = el("div", "xd-mctl");
  ctl.innerHTML =
    `<span class="t2m-chips">${["within", "between"].map((w) =>
      `<label class="seg"><input type="radio" name="xdwhich" value="${w}" ${w === t2x.which ? "checked" : ""}/><span>${w === "within" ? "within a person" : "between people"}</span></label>`).join("")}</span>` +
    `<span class="muted small">y taken ${r.lag === "same" ? "the same day" : r.lag === "night" ? "that night" : "the next day"} · ${r.n_tests} pairs tested</span>`;
  $$("input[name=xdwhich]", ctl).forEach((i) => i.addEventListener("change", (ev) => { t2x.which = ev.target.value; loadMatrix(); }));
  host.appendChild(ctl);

  const byPair = {};
  r.cells.forEach((c) => { byPair[`${c.x}|${c.y}`] = c; byPair[`${c.y}|${c.x}`] = c; });
  const vs = r.variables;
  const tbl = el("table", "tbl mtx");
  tbl.innerHTML = `<tr><th></th>${vs.map((v) => `<th title="${esc((r.meta[v] || {}).definition || "")}"><span class="mtx-h">${esc(v)}</span></th>`).join("")}</tr>` +
    vs.map((rv) => {
      const cells = vs.map((cv) => {
        if (rv === cv) return `<td class="mtx-diag"></td>`;
        const c = byPair[`${rv}|${cv}`];
        if (!c || c.r == null) return `<td class="mtx-na" title="${esc((c && c.note) || "not enough paired days")}">—</td>`;
        const a = Math.min(0.85, Math.abs(c.r) * 0.85);
        const bg = c.r >= 0 ? `rgba(79,140,255,${a})` : `rgba(245,130,90,${a})`;
        return `<td class="mtx-c ${c.q != null && c.q < 0.05 ? "sig" : ""}" style="background:${bg}" data-x="${esc(rv)}" data-y="${esc(cv)}" ` +
          `title="${esc(rv)} vs ${esc(cv)}\nr = ${c.r} ${c.ci ? `(95% CI ${c.ci[0]} to ${c.ci[1]})` : ""}\nq = ${c.q ?? "—"}\n${c.n_pairs} days, ${c.n_participants} people\nclick to plot">${c.r.toFixed(2)}</td>`;
      }).join("");
      return `<tr><th class="mtx-rh" title="${esc((r.meta[rv] || {}).definition || "")}">${esc(rv)}</th>${cells}</tr>`;
    }).join("");
  $$(".mtx-c", tbl).forEach((td) => td.addEventListener("click", () => {
    t2x.x = td.dataset.x; t2x.y = td.dataset.y;
    $("#xd-x").value = t2x.x; $("#xd-y").value = t2x.y;
    loadScatter();
    $(".xd-body").scrollIntoView({ block: "nearest" });
  }));
  host.appendChild(el("div", "mtx-wrap")).appendChild(tbl);
  host.appendChild(el("div", "muted small",
    "Blue = rises together, orange = one up while the other goes down. Bold survived the correction for testing many pairs at once. Exploratory."));

  const pick = el("details", "xd-mpick");
  const all = t2DayVariables();
  pick.innerHTML = `<summary>Which measures <span class="muted">${t2x.matrixVars.length} of ${all.length}, max 12</span></summary>` +
    `<div class="t2m-vargrid">${all.map((v) =>
      `<label class="t2m-var"><input type="checkbox" value="${v.id}" ${t2x.matrixVars.includes(v.id) ? "checked" : ""}/> ${esc(v.id)}</label>`).join("")}</div>`;
  $$("input", pick).forEach((i) => i.addEventListener("change", () => {
    const chosen = $$("input:checked", pick).map((c) => c.value);
    if (chosen.length > 12) { i.checked = false; showToast("mtx", "At most 12 measures", 100); setTimeout(() => hideToast("mtx"), 1600); return; }
    t2x.matrixVars = chosen;
    loadMatrix();
  }));
  host.appendChild(pick);
}

// ------------------------------------------------------------------ compare (P3)
const t2cmp = { variable: null, by: "season", who: "", editing: false };
const T2_COMPARE_BY = [["season", "season of the year"], ["group", "my groups"],
                       ["participant", "each participant"], ["season_n", "1st / 2nd / 3rd season"]];

async function renderCompare(host) {
  const vars = t2DayVariables();
  if (!vars.length) { host.appendChild(el("div", "muted", "No day-level measures in this selection.")); return; }
  if (!t2cmp.variable || !vars.some((v) => v.id === t2cmp.variable)) {
    t2cmp.variable = (vars.find((v) => v.id === "light_mel_mean") || vars[0]).id;
  }
  if (t2State.groups === null) {
    const g = await api(`/api/t2/selections/${encodeURIComponent(t2State.id)}/groups`).catch(() => null);
    t2State.groups = (g && g.groups) || {};
    t2State.people = (g && g.participants) || [];
  }
  const options = T2_DEVICES.map(([dev, label]) => {
    const opts = vars.filter((v) => v.device === dev)
      .map((v) => `<option value="${v.id}" ${v.id === t2cmp.variable ? "selected" : ""}>${esc(v.id)}${v.unit ? ` (${esc(v.unit)})` : ""}</option>`).join("");
    return opts ? `<optgroup label="${label}">${opts}</optgroup>` : "";
  }).join("");

  const ctl = el("div", "xd-ctl");
  ctl.innerHTML =
    `<label class="small muted">measure <select id="cmp-var">${options}</select></label>` +
    `<label class="small muted">split by <select id="cmp-by">${T2_COMPARE_BY.map(([k, l]) =>
      `<option value="${k}" ${k === t2cmp.by ? "selected" : ""}>${l}</option>`).join("")}</select></label>` +
    `<label class="small muted">people <select id="cmp-who"><option value="">everyone</option>` +
      (t2State.people || []).map((p) => `<option value="${esc(p)}" ${p === t2cmp.who ? "selected" : ""}>${esc(p)} only</option>`).join("") +
    `</select></label>` +
    `<button class="btn ghost tiny" id="cmp-groups">Edit groups</button>`;
  host.appendChild(ctl);
  host.appendChild(el("div", "cmp-groupbox"));
  host.appendChild(el("div", "cmp-body", `<div class="muted">summarising…</div>`));

  const reload = () => {
    t2cmp.variable = $("#cmp-var").value;
    t2cmp.by = $("#cmp-by").value;
    t2cmp.who = $("#cmp-who").value;
    loadCompare();
  };
  $("#cmp-var", ctl).addEventListener("change", reload);
  $("#cmp-by", ctl).addEventListener("change", reload);
  $("#cmp-who", ctl).addEventListener("change", reload);
  $("#cmp-groups", ctl).addEventListener("click", () => { t2cmp.editing = !t2cmp.editing; renderGroupEditor(); });
  renderGroupEditor();
  loadCompare();
}

// Groups are made here, by hand: no demographics, just "these people vs those".
function renderGroupEditor() {
  const host = $(".cmp-groupbox");
  if (!host) return;
  host.innerHTML = "";
  const groups = t2State.groups || {};
  const names = Object.keys(groups);
  if (!t2cmp.editing) {
    host.appendChild(el("div", "muted small", names.length
      ? `Groups: ${names.map((n) => `<b>${esc(n)}</b> (${groups[n].length})`).join(" · ")}`
      : "No groups yet — press <b>Edit groups</b> to put people into A and B."));
    return;
  }
  const box = el("div", "cmp-editor");
  box.appendChild(el("div", "muted small", "Click a person to move them between groups. Saved with this selection (privately)."));
  const assign = {};
  names.forEach((n) => groups[n].forEach((p) => { assign[p] = n; }));
  const letters = ["A", "B", "C", "D"];
  const grid = el("div", "cmp-people");
  (t2State.people || []).forEach((p) => {
    const cur = assign[p] || "";
    const b = el("button", `cmp-person ${cur ? "in" : ""}`, `${esc(p)} <span class="cmp-tag">${cur || "—"}</span>`);
    b.addEventListener("click", () => {
      const order = ["", ...letters];
      const next = order[(order.indexOf(cur) + 1) % order.length];
      const updated = {};
      names.concat(letters).forEach((n) => { updated[n] = (groups[n] || []).filter((x) => x !== p); });
      if (next) updated[next] = (updated[next] || []).concat([p]);
      Object.keys(updated).forEach((n) => { if (!updated[n].length) delete updated[n]; });
      saveGroups(updated);
    });
    grid.appendChild(b);
  });
  box.appendChild(grid);
  const done = el("button", "btn ghost tiny", "Done");
  done.addEventListener("click", () => { t2cmp.editing = false; renderGroupEditor(); });
  box.appendChild(done);
  host.appendChild(box);
}

async function saveGroups(groups) {
  const r = await postJSON(`/api/t2/selections/${encodeURIComponent(t2State.id)}/groups`, { groups })
    .catch(() => ({ error: "request failed" }));
  if (r.error) { window.alert(`Could not save groups: ${r.error}`); return; }
  t2State.groups = r.groups || {};
  renderGroupEditor();
  if (t2cmp.by === "group") loadCompare();
}

async function loadCompare() {
  const host = $(".cmp-body");
  if (!host) return;
  host.innerHTML = `<div class="muted">summarising…</div>`;
  const r = await postJSON("/api/t2/compare", {
    selection: t2State.id, variable: t2cmp.variable, by: t2cmp.by,
    participants: t2cmp.who ? [t2cmp.who] : [], ...t2Filters(),
  }).catch(() => ({ error: "request failed" }));
  host.innerHTML = "";
  if (r.error) { host.appendChild(el("div", "agg-note bad", esc(r.error))); return; }
  if (!r.rows.length) {
    host.appendChild(el("div", "agg-note bad", esc(r.note || "nothing to compare in this day range")));
    return;
  }
  const unit = (r.meta || {}).unit || "";
  host.appendChild(el("div", "xd-what",
    `<b>${esc(r.variable)}</b>${unit ? ` (${esc(unit)})` : ""}, split by <b>${esc(r.by_label)}</b>. ` +
    `Each bar is the average across people, with the range it could plausibly be. ` +
    `<span class="muted">${filterWords()}.</span>`));
  host.appendChild(drawCompareBars(r, unit));

  const tbl = el("table", "tbl");
  tbl.innerHTML = `<tr><th>${esc(r.by_label)}</th><th>average</th><th>spread (sd)</th><th>people</th><th>days</th></tr>` +
    r.rows.map((row) => `<tr><th class="mtx-rh">${esc(String(row.key))}</th><td>${fmtNum(row.mean)}` +
      `${row.ci ? ` <span class="muted small">(${fmtNum(row.ci[0])} to ${fmtNum(row.ci[1])})</span>` : ""}</td>` +
      `<td>${row.sd == null ? "—" : fmtNum(row.sd)}</td><td>${row.n_participants}</td><td>${row.n_days}</td></tr>`).join("");
  host.appendChild(tbl);

  const pairs = r.pairs.filter((p) => p.diff != null || p.paired_diff != null);
  if (pairs.length) {
    host.appendChild(el("h3", "sub", "Differences"));
    const dt = el("table", "tbl");
    dt.innerHTML = `<tr><th>comparison</th><th>difference</th><th>same people in both</th><th>q</th></tr>` +
      pairs.map((p) => {
        const paired = p.paired_diff != null;
        const d = paired ? p.paired_diff : p.diff;
        const ci = paired ? p.paired_ci : p.ci;
        return `<tr><td>${esc(String(p.a))} − ${esc(String(p.b))}</td>` +
          `<td class="${p.q != null && p.q < 0.05 ? "sig-cell" : ""}">${fmtNum(d)}${ci ? ` <span class="muted small">(${fmtNum(ci[0])} to ${fmtNum(ci[1])})</span>` : ""}</td>` +
          `<td>${p.n_shared_participants || 0}${paired ? " <span class=\"muted small\">(same-person difference)</span>" : ""}</td>` +
          `<td>${p.q == null ? "—" : p.q < 0.001 ? "< 0.001" : p.q.toFixed(3)}</td></tr>`;
      }).join("");
    host.appendChild(dt);
    host.appendChild(el("div", "muted small",
      "When the same people appear on both sides, the difference is worked out person by person. q allows for testing several comparisons."));
  }
}

function drawCompareBars(r, unit) {
  const rows = r.rows;
  const W = 700, rowH = 34, L = 150, R = 60, T = 10;
  const H = T + rows.length * rowH + 22;
  const lows = rows.map((x) => (x.ci ? x.ci[0] : x.mean)).filter((v) => v != null);
  const highs = rows.map((x) => (x.ci ? x.ci[1] : x.mean)).filter((v) => v != null);
  const lo = Math.min(0, ...lows), hi = Math.max(...highs);
  const px = (v) => L + ((v - lo) / ((hi - lo) || 1)) * (W - L - R);
  const parts = [];
  rows.forEach((row, i) => {
    const y = T + i * rowH + rowH / 2;
    const colour = `hsl(${(i * 61) % 360} 70% 60%)`;
    parts.push(`<text x="4" y="${y + 4}" class="cmp-lab">${esc(String(row.key))}</text>`);
    if (row.mean != null) {
      parts.push(`<rect x="${px(lo)}" y="${y - 7}" width="${Math.max(1, px(row.mean) - px(lo))}" height="14" rx="3" fill="${colour}" opacity=".55"/>`);
      if (row.ci) {
        parts.push(`<line x1="${px(row.ci[0])}" x2="${px(row.ci[1])}" y1="${y}" y2="${y}" class="cmp-ci"/>`);
        parts.push(`<line x1="${px(row.ci[0])}" x2="${px(row.ci[0])}" y1="${y - 5}" y2="${y + 5}" class="cmp-ci"/>`);
        parts.push(`<line x1="${px(row.ci[1])}" x2="${px(row.ci[1])}" y1="${y - 5}" y2="${y + 5}" class="cmp-ci"/>`);
      }
      parts.push(`<text x="${W - R + 6}" y="${y + 4}" class="cmp-val">${fmtNum(row.mean)}</text>`);
      parts.push(`<title>${esc(String(row.key))}: ${fmtNum(row.mean)} ${esc(unit)} · ${row.n_participants} people, ${row.n_days} days</title>`);
    }
  });
  parts.push(`<text x="${(L + W - R) / 2}" y="${H - 4}" class="sc-axis" text-anchor="middle">${esc(r.variable)}${unit ? ` (${esc(unit)})` : ""}</text>`);
  return el("div", "sc-wrap", `<svg class="cmp-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMinYMin meet">${parts.join("")}</svg>`);
}


// ------------------------------------------------------------------ overlap
// "Are these two the same?" - only answerable when both are in the same unit.
// Clock hours wrap, so 23:00 vs 01:00 is a 2 h gap, not 22.
const t2ov = { x: null, y: null, lag: "same", tolerance: 1, who: "" };

function renderOverlap(host) {
  const units = t2State.sameUnit || {};
  const unitNames = Object.keys(units).filter((u) => units[u].length > 1);
  if (!unitNames.length) {
    host.appendChild(el("div", "muted",
      "This selection has no two measures sharing a unit, so there is nothing to compare for agreement. " +
      "Use <b>Relationships</b> to compare measures on different scales."));
    return;
  }
  const unitOf = (id) => unitNames.find((u) => units[u].includes(id)) || unitNames[0];
  if (!t2ov.x || !unitNames.some((u) => units[u].includes(t2ov.x))) {
    const clock = units["clock hour"] || units[unitNames[0]];
    t2ov.x = clock[0];
    t2ov.y = clock[1];
  }
  const unit = unitOf(t2ov.x);
  if (!units[unit].includes(t2ov.y) || t2ov.y === t2ov.x) {
    t2ov.y = units[unit].find((v) => v !== t2ov.x);
  }

  const ctl = el("div", "xd-ctl");
  ctl.innerHTML =
    `<label class="small muted">unit <select id="ov-unit">${unitNames.map((u) =>
      `<option value="${esc(u)}" ${u === unit ? "selected" : ""}>${esc(u)}</option>`).join("")}</select></label>` +
    `<label class="small muted">this <select id="ov-x">${units[unit].map((v) =>
      `<option value="${v}" ${v === t2ov.x ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>` +
    `<label class="small muted">against <select id="ov-y">${units[unit].map((v) =>
      `<option value="${v}" ${v === t2ov.y ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>` +
    `<span class="t2m-chips">${T2_LAGS.map(([k, l]) =>
      `<label class="seg"><input type="radio" name="ovlag" value="${k}" ${k === t2ov.lag ? "checked" : ""}/><span>${l}</span></label>`).join("")}</span>` +
    `<label class="small muted">close enough <select id="ov-tol">${[0.5, 1, 1.5, 2, 3].map((t) =>
      `<option value="${t}" ${t === t2ov.tolerance ? "selected" : ""}>±${t} ${unit === "clock hour" ? "h" : ""}</option>`).join("")}</select></label>` +
    `<label class="small muted">person <select id="ov-who"><option value="">everyone</option>` +
      (t2State.people || []).map((p) => `<option value="${esc(p)}" ${p === t2ov.who ? "selected" : ""}>${esc(p)}</option>`).join("") +
    `</select></label>`;
  host.appendChild(ctl);
  host.appendChild(el("div", "ov-body", `<div class="muted">lining the days up…</div>`));

  const reload = () => {
    const u = $("#ov-unit").value;
    if (u !== unit) { t2ov.x = units[u][0]; t2ov.y = units[u][1]; renderT2View("overlap"); return; }
    t2ov.x = $("#ov-x").value;
    t2ov.y = $("#ov-y").value;
    t2ov.lag = ($("input[name=ovlag]:checked") || {}).value || "same";
    t2ov.tolerance = Number($("#ov-tol").value);
    t2ov.who = $("#ov-who").value;
    loadOverlap();
  };
  ["#ov-unit", "#ov-x", "#ov-y", "#ov-tol", "#ov-who"].forEach((sel) => $(sel, ctl).addEventListener("change", reload));
  $$("input[name=ovlag]", ctl).forEach((i) => i.addEventListener("change", reload));
  loadOverlap();
}

async function loadOverlap() {
  const host = $(".ov-body");
  if (!host) return;
  host.innerHTML = `<div class="muted">lining the days up…</div>`;
  const r = await postJSON("/api/t2/overlap", {
    selection: t2State.id, x: t2ov.x, y: t2ov.y, lag: t2ov.lag, tolerance: t2ov.tolerance, ...t2Filters(),
  }).catch(() => ({ error: "request failed" }));
  if (r.error) { host.innerHTML = `<div class="agg-note bad">${esc(r.error)}</div>`; return; }
  renderOverlapResult(r);
}

function renderOverlapResult(r) {
  const host = $(".ov-body");
  host.innerHTML = "";
  const s = r.stats;
  const days = t2ov.who ? r.days.filter((d) => d.display_id === t2ov.who) : r.days;
  if (!days.length) {
    host.appendChild(el("div", "agg-note bad", esc(s.note || "no days where both measures exist")));
    return;
  }
  const u = r.unit === "clock hour" ? "h" : (r.unit || "");
  host.appendChild(el("div", "xd-what",
    `<b>${esc(r.x.id)}</b> against <b>${esc(r.y.id)}</b>, both in ${esc(r.unit || "the same unit")}` +
    (r.lag === "same" ? "" : ` (${esc(r.lag_label)})`) +
    `. Same day, same line: how far apart are they?` +
    (r.circular ? " Hours wrap, so 23:00 and 01:00 are 2 h apart." : "")));

  const box = el("div", "xd-stats");
  box.innerHTML =
    `<div class="xd-stat"><div class="xd-slab">Land together<span class="muted">within ±${r.tolerance} ${u}</span></div>` +
    `<div class="xd-sval ${s.pct_within_tolerance >= 50 ? "strong" : ""}">${s.pct_within_tolerance}%</div>` +
    `<div class="muted small">${s.within_tolerance} of ${s.n_days} days</div></div>` +
    `<div class="xd-stat"><div class="xd-slab">Typical gap<span class="muted">median absolute difference</span></div>` +
    `<div class="xd-sval">${fmtNum(s.median_absolute_difference)} ${esc(u)}</div>` +
    `<div class="muted small">median difference ${fmtNum(s.median_difference)} ${esc(u)}</div></div>` +
    `<div class="xd-stat"><div class="xd-slab">Day-to-day spread<span class="muted">95% of differences sit here</span></div>` +
    `<div class="xd-sval">${s.limits_of_agreement ? `${fmtNum(s.limits_of_agreement[0])} to ${fmtNum(s.limits_of_agreement[1])}` : "—"}</div>` +
    `<div class="muted small">${s.n_participants} people, ${s.n_days} days</div></div>`;
  host.appendChild(box);
  host.appendChild(drawOverlap(r, days));

  if (s.per_participant.length > 1) {
    const tbl = el("table", "tbl");
    tbl.innerHTML = `<tr><th>participant</th><th>days</th><th>within ±${r.tolerance} ${esc(u)}</th><th>typical gap</th><th>median difference</th></tr>` +
      s.per_participant.map((p) => `<tr><th class="mtx-rh">${esc(p.display_id)}</th><td>${p.n_days}</td>` +
        `<td>${p.pct_within_tolerance}%</td><td>${fmtNum(p.median_absolute_difference)} ${esc(u)}</td>` +
        `<td>${fmtNum(p.median_difference)} ${esc(u)}</td></tr>`).join("");
    host.appendChild(tbl);
  }
}

// Both measures on ONE axis (they share a unit), one column per day, with the
// gap drawn between them. Same value = the two dots touch.
function drawOverlap(r, days) {
  const byPerson = {};
  days.forEach((d) => { (byPerson[d.display_id] = byPerson[d.display_id] || []).push(d); });
  const wrap = el("div", "sc-wrap");
  Object.entries(byPerson).forEach(([pid, rows]) => {
    rows.sort((a, b) => a.date.localeCompare(b.date));
    const W = 720, H = 190, L = 44, R = 12, T = 16, B = 34;
    const clock = r.unit === "clock hour";
    const vals = rows.flatMap((d) => [d.x, d.y]);
    const lo = clock ? 0 : Math.min(...vals), hi = clock ? 24 : Math.max(...vals);
    const py = (v) => H - B - ((v - lo) / ((hi - lo) || 1)) * (H - B - T);
    const px = (i) => L + (rows.length === 1 ? (W - L - R) / 2 : (i / (rows.length - 1)) * (W - L - R));
    const parts = [];
    for (let g = 0; g <= 4; g++) {
      const v = lo + (g / 4) * (hi - lo);
      parts.push(`<line x1="${L}" x2="${W - R}" y1="${py(v)}" y2="${py(v)}" class="sc-grid"/>`);
      parts.push(`<text x="${L - 6}" y="${py(v) + 3}" class="sc-lab" text-anchor="end">${clock ? `${Math.round(v)}:00` : fmtNum(v)}</text>`);
    }
    rows.forEach((d, i) => {
      const gap = r.circular ? ((d.x - d.y + 12) % 24) - 12 : d.x - d.y;
      const close = Math.abs(gap) <= r.tolerance;
      const cls = `ov-gap ${close ? "close" : ""}`;
      // A pair that wraps past midnight is CLOSE, so draw it the short way
      // (two stubs to the edges) instead of one long line across the chart.
      if (r.circular && Math.abs(d.x - d.y) > 12) {
        const up = d.x > d.y;
        parts.push(`<line x1="${px(i)}" x2="${px(i)}" y1="${py(d.x)}" y2="${py(up ? 24 : 0)}" class="${cls} wrap"/>`);
        parts.push(`<line x1="${px(i)}" x2="${px(i)}" y1="${py(up ? 0 : 24)}" y2="${py(d.y)}" class="${cls} wrap"/>`);
      } else {
        parts.push(`<line x1="${px(i)}" x2="${px(i)}" y1="${py(d.x)}" y2="${py(d.y)}" class="${cls}"/>`);
      }
      parts.push(`<circle cx="${px(i)}" cy="${py(d.x)}" r="3.2" fill="#f5b82e"><title>${d.date} · ${esc(r.x.id)} ${fmtNum(d.x)}</title></circle>`);
      parts.push(`<circle cx="${px(i)}" cy="${py(d.y)}" r="3.2" fill="#4f8cff"><title>${d.date} · ${esc(r.y.id)} ${fmtNum(d.y)} · gap ${fmtNum(gap)}</title></circle>`);
      if (i === 0 || i === rows.length - 1 || i % 4 === 0) {
        parts.push(`<text x="${px(i)}" y="${H - 14}" class="tl-tick">${d.date.slice(5)}</text>`);
      }
    });
    wrap.appendChild(el("div", "ov-person", `<b>${esc(pid)}</b> <span class="muted small">${rows.length} days</span>`));
    wrap.appendChild(el("div", null, `<svg class="ov-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMinYMin meet">${parts.join("")}</svg>`));
  });
  wrap.appendChild(el("div", "legend",
    `<span><i class="sw" style="background:#f5b82e;border-radius:50%"></i>${esc(t2ov.x)}</span>` +
    `<span><i class="sw" style="background:#4f8cff;border-radius:50%"></i>${esc(t2ov.y)}</span>` +
    `<span><i class="sw ov-sw-close"></i>within ±${t2ov.tolerance}</span>`));
  return wrap;
}


// ------------------------------------------------------------------ cut-offs (P4)
// A cut-off keeps days whose value for one measure meets a rule. It is applied
// to numbers the tools already produced, so nothing is reprocessed.
const t2cut = { ranges: null, editing: false, draft: null, info: null };

async function loadCutoffInfo() {
  const r = await postJSON("/api/t2/cutoffs", { selection: t2State.id, ...t2Filters() })
    .catch(() => null);
  if (!r || r.error) return null;
  t2cut.ranges = r.ranges;
  t2cut.info = r;
  return r;
}

function renderCutoffBar(host) {
  const bar = el("div", "t2-cuts");
  host.appendChild(bar);
  drawCutoffBar();
  loadCutoffInfo().then(() => drawCutoffBar());
}

function drawCutoffBar() {
  const bar = $(".t2-cuts");
  if (!bar) return;
  const info = t2cut.info;
  bar.innerHTML = `<span class="t2m-lbl">Cut-offs</span>` +
    `<span class="muted small">keep only days where a measure passes a rule — applied to the numbers as they are, nothing is recalculated</span>`;
  const chips = el("div", "t2m-chips");
  (t2State.cutoffs || []).forEach((c, i) => {
    const meta = (t2cut.ranges || []).find((r) => r.variable === c.variable) || {};
    const chip = el("span", "cut-chip",
      `${esc(c.variable)} ${esc(c.op)} <b>${c.value}</b>${meta.meta && meta.meta.unit ? ` ${esc(meta.meta.unit)}` : ""} <button class="cut-x" title="remove">✕</button>`);
    $(".cut-x", chip).addEventListener("click", () => {
      t2State.cutoffs.splice(i, 1);
      afterCutoffChange();
    });
    chips.appendChild(chip);
  });
  if (!(t2State.cutoffs || []).length) chips.appendChild(el("span", "muted small", "none — every day in the range is included"));
  bar.appendChild(chips);

  const add = el("button", "btn ghost tiny", t2cut.editing ? "close" : "+ add cut-off");
  add.addEventListener("click", () => { t2cut.editing = !t2cut.editing; drawCutoffBar(); });
  bar.appendChild(add);

  if (info) {
    const pct = info.before.days ? Math.round(100 * info.after.days / info.before.days) : 0;
    bar.appendChild(el("span", `cut-count ${info.after.days < info.before.days ? "on" : "muted"}`,
      `${info.after.days} of ${info.before.days} days kept (${pct}%) · ${info.after.participants} of ${info.before.participants} people`));
  }
  if (t2cut.editing) bar.appendChild(renderCutoffEditor());
}

function renderCutoffEditor() {
  const box = el("div", "cut-editor");
  const ranges = (t2cut.ranges || []).filter((r) => r.n_days > 1);
  if (!ranges.length) {
    box.appendChild(el("div", "muted small", "no measures with enough days to set a cut-off"));
    return box;
  }
  if (!t2cut.draft || !ranges.some((r) => r.variable === t2cut.draft.variable)) {
    const first = ranges[0];
    t2cut.draft = { variable: first.variable, op: ">=", value: first.median };
  }
  const draft = t2cut.draft;
  const cur = ranges.find((r) => r.variable === draft.variable);
  const step = Math.max(0.1, Math.round(((cur.max - cur.min) / 100) * 10) / 10);
  box.innerHTML =
    `<label class="small muted">keep days where <select id="cut-var">${T2_DEVICES.map(([dev, label]) => {
      const opts = ranges.filter((r) => r.device === dev).map((r) =>
        `<option value="${r.variable}" ${r.variable === draft.variable ? "selected" : ""}>${esc(r.variable)}</option>`).join("");
      return opts ? `<optgroup label="${label}">${opts}</optgroup>` : "";
    }).join("")}</select></label>` +
    `<select id="cut-op">${Object.entries((t2cut.info || {}).operators || { ">=": "at least", "<=": "at most" })
      .map(([k, l]) => `<option value="${k}" ${k === draft.op ? "selected" : ""}>${esc(l)} (${k})</option>`).join("")}</select>` +
    `<input type="range" id="cut-val" min="${cur.min}" max="${cur.max}" step="${step}" value="${draft.value}">` +
    `<span id="cut-valtext" class="cut-val">${draft.value}${cur.meta && cur.meta.unit ? ` ${esc(cur.meta.unit)}` : ""}</span>` +
    `<span class="muted small">this measure runs ${fmtNum(cur.min)} to ${fmtNum(cur.max)}, middle ${fmtNum(cur.median)}</span>` +
    `<button class="btn tiny" id="cut-apply">apply</button>`;
  $("#cut-var", box).addEventListener("change", (ev) => {
    const r = ranges.find((x) => x.variable === ev.target.value);
    t2cut.draft = { variable: r.variable, op: draft.op, value: r.median };
    drawCutoffBar();
  });
  $("#cut-op", box).addEventListener("change", (ev) => { draft.op = ev.target.value; });
  $("#cut-val", box).addEventListener("input", (ev) => {
    draft.value = Number(ev.target.value);
    $("#cut-valtext").textContent = `${draft.value}${cur.meta && cur.meta.unit ? ` ${cur.meta.unit}` : ""}`;
  });
  $("#cut-apply", box).addEventListener("click", () => {
    t2State.cutoffs = (t2State.cutoffs || []).filter((c) => c.variable !== draft.variable);
    t2State.cutoffs.push({ ...draft });
    t2cut.editing = false;
    afterCutoffChange();
  });

  const needs = ((t2cut.info || {}).needs_reprocessing) || [];
  if (needs.length) {
    const det = el("details", "cut-baked");
    det.innerHTML = `<summary class="muted small">Thresholds a slider here cannot move (${needs.length})</summary>` +
      `<div class="muted small">These are built into the numbers themselves, so changing one means re-running the device tools from the raw data — a job for the Compliance panel, not T2.</div>` +
      `<ul class="cut-list">${needs.map((n) =>
        `<li><b>${esc(n.name)}</b> → ${esc(n.affects)} <span class="muted">(${esc(n.where)})</span></li>`).join("")}</ul>`;
    box.appendChild(det);
  }
  return box;
}

function afterCutoffChange() {
  loadCutoffInfo().then(() => {
    drawCutoffBar();
    renderT2View(t2State.view);
  });
}

// ------------------------------------------------------------------ export (P5)
async function openExport() {
  const modal = $("#t2x-modal");
  modal.classList.remove("hidden");
  $("#t2x-body").innerHTML = `<div class="muted">working out what would be written…</div>`;
  $("#t2x-note").textContent = "";
  $("#t2x-initials").value = currentInitials() || "";
  updateExportState();
  const r = await postJSON("/api/t2/export/preview", { selection: t2State.id, ...t2Filters() })
    .catch(() => ({ error: "request failed" }));
  if (r.error) { $("#t2x-body").innerHTML = `<div class="agg-note bad">${esc(r.error)}</div>`; return; }
  t2State.exportPreview = r;
  renderExportPreview(r);
  updateExportState();
}

function renderExportPreview(r) {
  const host = $("#t2x-body");
  host.innerHTML = "";
  const inc = r.inclusion;
  host.appendChild(el("div", "xd-what",
    `This writes <b>${r.files.length} files</b> to <code>${esc(r.t2_root)}</code>` +
    (r.t2_root_exists ? "" : ` <span class="hint bad">— that folder does not exist yet; it will be created</span>`) +
    `. Nothing is written until you press Export.`));

  const stats = el("div", "stat-row");
  stats.appendChild(el("div", "stat", `<div class="n">${inc.participants}</div><div class="k">participants</div>`));
  stats.appendChild(el("div", "stat", `<div class="n">${inc.participant_seasons}</div><div class="k">participant-seasons</div>`));
  stats.appendChild(el("div", "stat", `<div class="n">${inc.days}</div><div class="k">days</div>`));
  stats.appendChild(el("div", "stat", `<div class="n">${inc.measures}</div><div class="k">measures</div>`));
  host.appendChild(stats);

  host.appendChild(el("h3", "sub", "Files that will be written"));
  const tbl = el("table", "tbl");
  tbl.innerHTML = `<tr><th>file</th><th>rows</th><th>columns</th><th>what it is</th></tr>` +
    r.files.map((f) => `<tr><td class="mono">${esc(f.name)}</td>` +
      `<td>${f.rows == null ? "—" : f.rows}</td><td>${f.columns == null ? "—" : f.columns}</td>` +
      `<td class="small">${esc(f.what)}</td></tr>`).join("");
  host.appendChild(tbl);

  host.appendChild(el("h3", "sub", "What is included"));
  const rows = [
    ["seasons", (inc.seasons || []).join(", ") || "—"],
    ["devices", (inc.devices || []).join(", ")],
    ["day range", inc.day_range ? `days ${inc.day_range[0]}–${inc.day_range[1]} of each shared window` : "all days"],
    ["part-days", inc.part_days],
    ["cut-offs", (inc.cut_offs || []).length
      ? inc.cut_offs.map((c) => `${c.variable} ${c.op} ${c.value}`).join(" · ") : "none"],
  ];
  const t2 = el("table", "tbl");
  t2.innerHTML = rows.map(([k, v]) => `<tr><th class="mtx-rh">${esc(k)}</th><td>${esc(String(v))}</td></tr>`).join("");
  host.appendChild(t2);
  if (r.empty) host.appendChild(el("div", "agg-note bad", "Nothing to export in the current range."));
}

function updateExportState() {
  const ok = /^[A-Z]{2,4}$/.test($("#t2x-initials").value) &&
             t2State.exportPreview && !t2State.exportPreview.empty;
  $("#t2x-go").disabled = !ok;
  $("#t2x-go").classList.toggle("ready", !!ok);
  $("#t2x-todo").innerHTML = ok ? `<span class="todo-ok">Ready.</span>`
    : `<span class="todo-x">Still to do:</span> ${/^[A-Z]{2,4}$/.test($("#t2x-initials").value) ? "nothing to export" : "approver's initials"}`;
}

async function runExport() {
  const btn = $("#t2x-go");
  btn.disabled = true;
  $("#t2x-note").textContent = "writing…";
  const r = await postJSON("/api/t2/export", {
    selection: t2State.id, approver: $("#t2x-initials").value, ...t2Filters(),
  }).catch(() => ({ error: "request failed" }));
  if (r.error) { $("#t2x-note").innerHTML = `<span class="hint bad">${esc(r.error)}</span>`; btn.disabled = false; return; }
  $("#initials").value = $("#t2x-initials").value;
  const host = $("#t2x-body");
  host.innerHTML = "";
  host.appendChild(el("div", "agg-note ok",
    `<b>Exported.</b> ${r.files.length} files written, approved by ${esc(r.approved_by)}.`));
  host.appendChild(el("div", "xd-what", `<code>${esc(r.destination)}</code>`));
  const tbl = el("table", "tbl");
  tbl.innerHTML = `<tr><th>file</th><th>size</th></tr>` + r.files.map((f) =>
    `<tr><td class="mono">${esc(f.name)}</td><td>${fmtBytes(f.bytes)}</td></tr>`).join("");
  host.appendChild(tbl);
  $("#t2x-note").textContent = "";
  $("#t2x-go").textContent = "Export again";
  $("#t2x-go").disabled = false;
  loadT2Selections();
}
