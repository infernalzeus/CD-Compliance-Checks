"""F1 — a compliance report over a selection of folders, or over one batch.

What the dashboard already had was per-item: a verdict on a recording, a row in
a participant's summary CSV. What it did not have was the thing you send to
someone: *across these folders, where does the study stand, and what is still
outstanding.* That is this.

Two outputs from one build:

    report(...)  ->  a dict, the whole report as data
      render_html(...)  a self-contained page to read and print
      write_xlsx(...)   a workbook to send on

Written to ``runs/_private/reports/``. Reports name participants, so they are
**private by construction** - that folder is git-ignored, the same place the
unstripped batch detail lives. Nothing here ever goes near ``runs/batches/``,
which is public and stripped.
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from . import manifest, naming, results

#: Order severities are reported in. "skipped" first: it is the only one that
#: stops a file being processed at all.
_SEV_RANK = {"skipped": 0, "warning": 1, "info": 2}

#: How each severity is worded for a reader who did not write the checker.
SEV_WORD = {"skipped": "NOT READ", "warning": "warning", "info": "note"}
from .config import Config
from .devices import implemented_devices

#: Beside the batch detail, not among it - see batches.private_dir.
REPORTS_DIRNAME = Path("_private") / "reports"

#: Verdicts, worst first - the order they are counted and shown in.
VERDICTS = ("FAIL", "ERROR", "REVIEW", "PASS")


def reports_dir(config: Config) -> Path:
    return Path(config.runs_dir) / REPORTS_DIRNAME


def _pct(values: list[float]) -> Optional[float]:
    return round(sum(values) / len(values), 1) if values else None


def _verdict_counts(items: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = {v: 0 for v in VERDICTS}
    for it in items:
        v = (it.get("verdict") or "").upper()
        if v in counts:
            counts[v] += 1
    return counts


def build(config: Config, participants: list[str],
          devices: Optional[list[str]] = None,
          batch_id: Optional[str] = None,
          title: str = "") -> dict[str, Any]:
    """Collect the report.

    Three questions, in the order anyone actually asks them:

    1. **Where does the study stand** - verdicts by device and by season.
    2. **Who is outstanding** - which participant-seasons still need work, and
       which device is the reason.
    3. **What is wrong with the inputs** - the naming problems, since a file
       that is skipped never gets a verdict at all and would otherwise be
       invisible in a report about verdicts.
    """
    wanted = [d.lower() for d in (devices or implemented_devices())]
    rows: list[dict[str, Any]] = []
    outstanding: list[dict[str, Any]] = []
    missing_seasons: list[dict[str, Any]] = []

    for pid in participants:
        items = [it for it in results.output_items(config, pid)
                 if it["device"] in wanted]
        rows.extend(items)
        try:
            board = manifest.season_board(config, pid, wanted)
        except FileNotFoundError:
            continue
        for season in board["seasons"]:
            for dev in season["devices"]:
                if not dev["implemented"] or not dev["has_input"]:
                    continue
                if dev["state"] != "complete":
                    outstanding.append({
                        "participant": pid, "season": season["label"],
                        "folder": season["season"], "n": season["n"],
                        "device": dev["device"], "state": dev["state"],
                        "done": dev["n_complete"], "of": dev["n_inputs"],
                    })
        short = (board.get("expected_seasons") or 4) - len(board["seasons"])
        if short > 0:
            missing_seasons.append({"participant": pid, "missing": short,
                                    "has": len(board["seasons"])})

    # --- by device -----------------------------------------------------------
    by_device: dict[str, Any] = {}
    for it in rows:
        d = by_device.setdefault(it["device"], {"device": it["device"], "n": 0,
                                                "pct": [], "items": []})
        d["n"] += 1
        d["items"].append(it)
        pct = it.get("pct_compliance_mean")
        if isinstance(pct, (int, float)):
            d["pct"].append(float(pct))
    devices_out = []
    for d in sorted(by_device.values(), key=lambda x: x["device"]):
        devices_out.append({"device": d["device"], "n": d["n"],
                            "pct_compliance_mean": _pct(d["pct"]),
                            **_verdict_counts(d["items"])})

    # --- by season (calendar, so participants line up) -----------------------
    by_season: dict[str, Any] = {}
    for it in rows:
        key = it.get("season_type") or it["season"]
        s = by_season.setdefault(key, {"season": key, "n": 0, "pct": [],
                                       "participants": set(), "items": []})
        s["n"] += 1
        s["items"].append(it)
        s["participants"].add(it["participant"])
        pct = it.get("pct_compliance_mean")
        if isinstance(pct, (int, float)):
            s["pct"].append(float(pct))
    seasons_out = []
    for s in sorted(by_season.values(), key=lambda x: x["season"]):
        seasons_out.append({"season": s["season"], "n": s["n"],
                            "participants": len(s["participants"]),
                            "pct_compliance_mean": _pct(s["pct"]),
                            **_verdict_counts(s["items"])})

    # --- naming ---------------------------------------------------------------
    # Worst first everywhere: a file that cannot be read never gets a verdict,
    # a warning still gets one.
    flags: list[dict[str, Any]] = []
    for pid in participants:
        for f in naming.check_participant(config, pid):
            flags.append(f if isinstance(f, dict) else getattr(f, "__dict__", {"message": str(f)}))
    flag_counts: dict[str, int] = {}
    for f in flags:
        sev = str(f.get("severity") or "info")
        flag_counts[sev] = flag_counts.get(sev, 0) + 1

    # --- one row per participant: the question the report is really for ------
    people: list[dict[str, Any]] = []
    for pid in participants:
        mine = [r for r in rows if r["participant"] == pid]
        out_for = [o for o in outstanding if o["participant"] == pid]
        try:
            board = manifest.season_board(config, pid, wanted)
            seasons_n = len(board["seasons"])
        except FileNotFoundError:
            board, seasons_n = None, 0
        try:
            status = manifest.participant_status(config, pid, wanted)
        except FileNotFoundError:
            # Selected, but there is no staging folder for them. That is a real
            # state worth reporting, not a reason to leave them out of the
            # report they were selected for.
            people.append({
                "participant": pid, "data_state": "none", "data_label": "no staging folder",
                "devices_present": [], "devices_missing": [], "n_devices_expected": 0,
                "seasons": seasons_n, "processing": "empty", "done": 0, "items": 0, "todo": 0,
                "verdicts": _verdict_counts(mine), "n_recordings": len(mine),
                "pct_compliance_mean": None, "blocking_flags": 0, "naming_warnings": 0,
                "naming_flags": [], "naming_info": [],
                "season_gaps": [],
                "outstanding": len(out_for), "ready": False,
                "why_not_ready": ["no staging folder for this participant"],
                "season_rows": [],
            })
            continue

        flags_mine = [f for f in flags if f.get("participant") == pid]
        n_block = sum(1 for f in flags_mine if f.get("severity") == "skipped")
        n_warn = sum(1 for f in flags_mine if f.get("severity") == "warning")

        if status.data_state == "full":
            data_label = "complete"
        elif status.data_state == "none":
            data_label = "no data"
        else:
            data_label = f"missing {', '.join(status.devices_missing)}"

        # "Ready" is the decision the grid colour encodes, spelled out.
        reasons = []
        if status.data_state != "full":
            reasons.append("data incomplete")
        if n_block:
            reasons.append(f"{n_block} file(s) cannot be processed")
        if status.total == 0:
            reasons.append("nothing to process")
        ready = not reasons
        todo = status.total - status.done
        people.append({
            "participant": pid,
            "data_state": status.data_state,
            "data_label": data_label,
            "devices_present": status.devices_present,
            "devices_missing": status.devices_missing,
            "n_devices_expected": status.n_devices_expected,
            # Seasons that have data and are short of a device - the table's
            # "short" column, and the thing a device count alone cannot show.
            "season_gaps": status.season_gaps,
            "seasons": seasons_n,
            "processing": status.state,
            "done": status.done, "items": status.total, "todo": todo,
            "verdicts": _verdict_counts(mine),
            "n_recordings": len(mine),
            "pct_compliance_mean": _pct([float(r["pct_compliance_mean"]) for r in mine
                                         if isinstance(r.get("pct_compliance_mean"), (int, float))]),
            "blocking_flags": n_block,
            "naming_warnings": n_warn,
            # The flags themselves, worst first, so the report can say WHICH
            # file rather than how many.
            "naming_flags": sorted(
                (f for f in flags_mine if f.get("severity") != "info"),
                key=lambda f: (_SEV_RANK.get(str(f.get("severity")), 9),
                               str(f.get("season") or ""), str(f.get("file") or ""))),
            "naming_info": [f for f in flags_mine if f.get("severity") == "info"],
            "outstanding": len(out_for),
            "ready": ready,
            "why_not_ready": reasons,
            "season_rows": [
                {"n": sn.get("n"), "label": sn["label"], "season": sn["season"],
                 "state": sn["state"],
                 "devices": [{"device": d["device"], "state": d["state"],
                              "done": d["n_complete"], "of": d["n_inputs"],
                              "verdicts": [v for v in (d.get("verdicts") or []) if v]}
                             for d in sn["devices"] if d["implemented"]]}
                for sn in (board["seasons"] if board else [])],
        })

    totals = _verdict_counts(rows)
    report = {
        "people": people,
        "n_ready": sum(1 for x in people if x["ready"]),
        "n_data_complete": sum(1 for x in people if x["data_state"] == "full"),
        "n_blocked": sum(1 for x in people if x["blocking_flags"]),
        "schema_version": 1,
        "title": title or ("Compliance report"
                           + (f" — batch {batch_id}" if batch_id else "")),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "batch_id": batch_id,
        "participants": list(participants),
        "n_participants": len(participants),
        "devices_requested": wanted,
        "n_recordings": len(rows),
        "verdicts": totals,
        "pct_compliance_mean": _pct([float(it["pct_compliance_mean"]) for it in rows
                                     if isinstance(it.get("pct_compliance_mean"), (int, float))]),
        "by_device": devices_out,
        "by_season": seasons_out,
        "outstanding": sorted(outstanding, key=lambda r: (r["participant"], r["n"] or 0,
                                                          r["device"])),
        "missing_seasons": missing_seasons,
        "naming": {"counts": flag_counts, "flags": flags},
        "rows": sorted(rows, key=lambda r: (r["participant"], r["season"], r["device"])),
        "output_root": str(config.paths.output_root),
        "source_root": str(config.paths.source_root),
    }
    return report


# ----------------------------------------------------------------------- HTML


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _table(headers: list[str], rows: list[list[Any]], cls: str = "") -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c if isinstance(c, str) and c.startswith('<') else _e(c)}</td>"
                                    for c in r) + "</tr>" for r in rows)
    return (f'<table class="{cls}"><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table>")


def _chip(verdict: str, n: int) -> str:
    if not n:
        return '<span class="z">0</span>'
    return f'<span class="v {_e(verdict)}">{n}</span>'


def _ready_chip(person: dict[str, Any]) -> str:
    if person["ready"]:
        return '<span class="r yes">ready</span>'
    return ('<span class="r no">not ready</span><span class="why">'
            + _e(" \u00b7 ".join(person["why_not_ready"])) + "</span>")


def _data_chip(person: dict[str, Any]) -> str:
    cls = {"full": "yes", "partial": "part", "sparse": "no", "none": "no"}.get(
        person["data_state"], "no")
    n = len(person["devices_present"])
    return (f'<span class="r {cls}">{n}/{person["n_devices_expected"]}</span>'
            f'<span class="why">{_e(person["data_label"])}</span>')


def _proc_chip(person: dict[str, Any]) -> str:
    label = {"complete": "all done", "partial": "part done",
             "pending": "not started", "empty": "nothing to do"}.get(
                 person["processing"], person["processing"])
    cls = {"complete": "yes", "partial": "part", "pending": "no"}.get(
        person["processing"], "none")
    extra = f'{person["done"]}/{person["items"]}' if person["items"] else ""
    return f'<span class="r {cls}">{_e(label)}</span><span class="why">{extra}</span>'


def _person_block(person: dict[str, Any]) -> str:
    """One participant: the decision row, then its seasons underneath."""
    # Which seasons carry a naming problem, so the row can say so rather than
    # leaving the reader to match a filename against a folder.
    by_season: dict[str, str] = {}
    for f in person.get("naming_flags", []):
        key = str(f.get("season") or "")
        if str(f.get("severity")) == "skipped" or key not in by_season:
            by_season[key] = str(f.get("severity"))

    seasons = ""
    for sn in person["season_rows"]:
        devs = "".join(
            f'<span class="d {_e(d["state"])}" title="{_e(d["device"])}: {_e(d["state"])}'
            f' ({d["done"]}/{d["of"]})">{_e(d["device"][:3])}'
            f'<i>{d["done"]}/{d["of"]}</i></span>'
            for d in sn["devices"])
        mark = (by_season.get(str(sn.get("season") or ""))
                or by_season.get(str(sn.get("label") or "")) or "")
        seasons += (f'<tr><td class="sn">S{sn["n"] or "?"}</td>'
                    f'<td>{_e(sn["label"])}'
                    + (f'<b class="nmark {mark}" title="see the naming table below">'
                       f'{"\u2715" if mark == "skipped" else "\u26a0"}</b>' if mark else "")
                    + "</td>"
                    f'<td class="state {_e(sn["state"])}">{_e(sn["state"].replace("_", " "))}</td>'
                    f"<td>{devs}</td></tr>")
    if not seasons:
        seasons = '<tr><td colspan="4" class="none">No season folders yet.</td></tr>'

    # The naming problems, named. A count alone sends the reader back to the
    # dashboard to find out which file it meant.
    naming_rows = "".join(
        f'<tr class="nm {_e(str(f.get("severity") or ""))}">'
        f'<td class="nsev">{_e(SEV_WORD.get(str(f.get("severity")), str(f.get("severity") or "")))}</td>'
        f'<td>{_e(str(f.get("season") or "\u2014"))}'
        f'{" \u00b7 " + _e(str(f["device"])) if f.get("device") else ""}</td>'
        f'<td class="nfile">{_e(str(f.get("file") or f.get("name") or ""))}</td>'
        f'<td>{_e(str(f.get("message") or ""))}'
        f'{"<br><span class=\'why\'>expected <code>" + _e(str(f["expected"])) + "</code></span>" if f.get("expected") else ""}'
        f"</td></tr>"
        for f in person.get("naming_flags", []))
    naming_block = (
        '<table class="naming"><thead><tr><th></th><th>season</th><th>file</th>'
        "<th>what is wrong</th></tr></thead><tbody>" + naming_rows + "</tbody></table>"
        + (f'<div class="ninfo">{len(person.get("naming_info", []))} cosmetic naming '
           "note(s) as well \u2014 they change nothing.</div>"
           if person.get("naming_info") else "")
    ) if naming_rows else ""
    v = person["verdicts"]
    return f"""
<details class="person{'' if person['ready'] else ' flagged'}" {'open' if not person['ready'] else ''}>
  <summary>
    <b class="pid">{_e(person['participant'])}</b>
    {_ready_chip(person)}
    <span class="cols">
      <span><em>data</em>{_data_chip(person)}</span>
      <span><em>processing</em>{_proc_chip(person)}</span>
      <span><em>verdicts</em>
        <span class="v PASS">{v['PASS']}</span><span class="v REVIEW">{v['REVIEW']}</span>
        <span class="v FAIL">{v['FAIL'] + v['ERROR']}</span></span>
      <span><em>naming</em>{
        ('<span class="r no">' + str(person['blocking_flags']) + ' blocking</span>')
        if person['blocking_flags'] else
        ('<span class="r part">' + str(person['naming_warnings']) + ' warning(s)</span>')
        if person['naming_warnings'] else '<span class="r yes">clean</span>'}</span>
    </span>
  </summary>
  <table class="seasons">
    <thead><tr><th></th><th>season</th><th>state</th><th>devices \u00b7 processed / found</th></tr></thead>
    <tbody>{seasons}</tbody>
  </table>
  {naming_block}
</details>"""


#: Table columns, in reading order. (key, heading, kind) - `kind` decides
#: alignment and how the sort compares it. One definition, used for the header,
#: the body and the sort, so a column cannot end up in two different places.
_COLS = [
    ("pid",     "folder",       "text"),
    ("ready",   "ready",        "state"),
    ("why",     "why not",      "text"),
    ("data",    "devices",      "num"),
    ("missing", "missing",      "text"),
    ("seasons", "seasons",      "num"),
    ("short",   "short",        "num"),
    ("proc",    "processing",   "state"),
    ("done",    "done",         "num"),
    ("items",   "items",        "num"),
    ("todo",    "to do",        "num"),
    ("pass",    "pass",         "num"),
    ("review",  "review",       "num"),
    ("fail",    "fail",         "num"),
    ("pct",     "%compliance",  "num"),
    ("notread", "not read",     "num"),
    ("warn",    "warnings",     "num"),
]


def _row_cells(x: dict[str, Any]) -> dict[str, tuple[str, Any]]:
    """One participant as (html, sort-value) per column."""
    v = x["verdicts"]
    n_short = len([g for g in x.get("season_gaps", []) if not g.get("empty")])
    pct = x["pct_compliance_mean"]
    ready_cls = "yes" if x["ready"] else "no"
    proc_cls = {"complete": "yes", "partial": "part", "pending": "no"}.get(x["processing"], "none")
    proc_lbl = {"complete": "done", "partial": "part", "pending": "not started",
                "empty": "nothing"}.get(x["processing"], x["processing"])
    data_cls = {"full": "yes", "partial": "part", "sparse": "no", "none": "no"}.get(
        x["data_state"], "no")
    return {
        "pid":     (f'<b>{_e(x["participant"])}</b>', x["participant"]),
        "ready":   (f'<span class="r {ready_cls}">{"ready" if x["ready"] else "no"}</span>',
                    0 if x["ready"] else 1),
        "why":     (_e(" \u00b7 ".join(x["why_not_ready"])), " ".join(x["why_not_ready"])),
        "data":    (f'<span class="r {data_cls}">{len(x["devices_present"])}/'
                    f'{x["n_devices_expected"]}</span>', len(x["devices_present"])),
        "missing": (_e(", ".join(x["devices_missing"])), ", ".join(x["devices_missing"])),
        "seasons": (str(x["seasons"]), x["seasons"]),
        "short":   (f'<span class="r part">{n_short}</span>' if n_short else
                    '<span class="z">0</span>', n_short),
        "proc":    (f'<span class="r {proc_cls}">{_e(proc_lbl)}</span>', proc_lbl),
        "done":    (str(x["done"]), x["done"]),
        "items":   (str(x["items"]), x["items"]),
        "todo":    (f'<b>{x["todo"]}</b>' if x["todo"] else '<span class="z">0</span>', x["todo"]),
        "pass":    (_chip("PASS", v["PASS"]), v["PASS"]),
        "review":  (_chip("REVIEW", v["REVIEW"]), v["REVIEW"]),
        "fail":    (_chip("FAIL", v["FAIL"] + v["ERROR"]), v["FAIL"] + v["ERROR"]),
        "pct":     ("\u2014" if pct is None else f"{pct}%", -1 if pct is None else pct),
        "notread": (f'<span class="r no">{x["blocking_flags"]}</span>'
                    if x["blocking_flags"] else '<span class="z">0</span>', x["blocking_flags"]),
        "warn":    (f'<span class="r part">{x["naming_warnings"]}</span>'
                    if x["naming_warnings"] else '<span class="z">0</span>', x["naming_warnings"]),
    }


def _table_view(people: list[dict[str, Any]]) -> str:
    """The default view: every participant, one line each, sortable."""
    head = "".join(
        f'<th class="k-{kind}" data-col="{i}" data-kind="{kind}" '
        f'title="Click to sort by {_e(label)}">{_e(label)}</th>'
        for i, (_key, label, kind) in enumerate(_COLS))
    body = []
    for x in people:
        cells = _row_cells(x)
        tds = "".join(
            f'<td class="k-{kind}" data-v="{_e(cells[key][1])}">{cells[key][0]}</td>'
            for key, _label, kind in _COLS)
        body.append(f'<tr class="{"" if x["ready"] else "flagged"}" '
                    f'data-pid="{_e(x["participant"])}">{tds}</tr>')
    return (f'<table class="grid" id="ptable"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table>')


def render_html(report: dict[str, Any]) -> str:
    """A page that opens on a table and keeps the per-folder detail behind it."""
    # Folders that hold something come first - a first screen of "nothing to
    # process" is a wasted screen - then not-ready before ready, then the
    # folder number. Every column is clickable, so this only has to be a
    # sensible place to start.
    people = sorted(report["people"],
                    key=lambda x: (0 if x["items"] else 1, x["ready"], x["participant"]))
    v = report["verdicts"]
    not_ready = [x for x in people if not x["ready"]]
    flag_counts = report["naming"]["counts"]
    blocks = "".join(_person_block(x) for x in people)

    return f"""<!doctype html>
<meta charset="utf-8">
<title>{_e(report['title'])}</title>
<style>
  body {{ font: 12.5px/1.45 "Segoe UI", system-ui, sans-serif; color: #1b1f28;
         background: #fff; margin: 0; padding: 20px 24px 50px; }}
  h1 {{ font-size: 18px; margin: 0 0 2px; }}
  h2 {{ font-size: 11.5px; text-transform: uppercase; letter-spacing: .8px;
       color: #5a6475; margin: 22px 0 8px; }}
  .sub {{ color: #5a6475; font-size: 11.5px; margin-bottom: 12px; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 7px; margin-bottom: 4px; }}
  .card {{ border: 1px solid #d8dde6; border-radius: 8px; padding: 5px 11px; min-width: 76px; }}
  .card b {{ display: block; font-size: 16px; line-height: 1.2; }}
  .card span {{ font-size: 10px; color: #5a6475; }}
  .card.go {{ border-color: #9ad6b0; background: #f3fbf6; }}
  .card.stop {{ border-color: #e6b4b4; background: #fdf5f5; }}

  /* ---- the table: dense enough that forty folders is one screen ---- */
  .bar {{ display: flex; align-items: center; gap: 8px; margin: 14px 0 6px; }}
  .bar button {{ font: inherit; font-size: 11px; padding: 3px 11px; cursor: pointer;
                border: 1px solid #d8dde6; background: #fff; border-radius: 999px;
                color: #3b4354; }}
  .bar button.on {{ background: #1b1f28; border-color: #1b1f28; color: #fff; }}
  .bar input {{ font: inherit; font-size: 11px; padding: 3px 9px; border-radius: 6px;
               border: 1px solid #d8dde6; width: 150px; }}
  .bar .hint {{ color: #8b93a3; font-size: 11px; margin-left: auto; }}

  table.grid {{ border-collapse: collapse; width: 100%; font-size: 11.5px; }}
  table.grid th {{ position: sticky; top: 0; background: #eef1f6; text-align: left;
                  font-weight: 600; color: #3b4354; padding: 5px 7px; cursor: pointer;
                  white-space: nowrap; border-bottom: 1px solid #d8dde6;
                  font-size: 10px; text-transform: uppercase; letter-spacing: .4px; }}
  table.grid th:hover {{ background: #e3e8f1; }}
  table.grid th.sorted::after {{ content: " \u25be"; }}
  table.grid th.sorted.up::after {{ content: " \u25b4"; }}
  table.grid td {{ padding: 3px 7px; border-bottom: 1px solid #f0f2f6;
                  white-space: nowrap; }}
  table.grid td.k-num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  table.grid tbody tr:nth-child(even) {{ background: #fbfcfe; }}
  table.grid tbody tr:hover {{ background: #eef4ff; }}
  table.grid tr.flagged td:first-child {{ box-shadow: inset 3px 0 0 #e3a93c; }}
  table.grid tr.hide {{ display: none; }}
  td.k-text {{ max-width: 230px; overflow: hidden; text-overflow: ellipsis;
              color: #5a6475; }}

  .r {{ font-size: 10.5px; font-weight: 700; padding: 1px 7px; border-radius: 999px; }}
  .r.yes {{ background: #dcf5e4; color: #176b35; }}
  .r.part {{ background: #fdf0d2; color: #7a5410; }}
  .r.no {{ background: #fbdcdc; color: #8a1f1f; }}
  .r.none {{ background: #eef0f4; color: #5a6475; }}
  .z {{ color: #c3c9d4; }}
  .v {{ display: inline-block; min-width: 15px; text-align: center; padding: 0 5px;
       border-radius: 999px; font-size: 10.5px; font-weight: 700; }}
  .v.PASS {{ background: #dcf5e4; color: #176b35; }}
  .v.REVIEW {{ background: #fdf0d2; color: #7a5410; }}
  .v.FAIL {{ background: #fbdcdc; color: #8a1f1f; }}
  .why {{ font-size: 10.5px; color: #5a6475; }}

  /* ---- detail view, behind the toggle ---- */
  #detail {{ display: none; }}
  body.detail #detail {{ display: block; }}
  body.detail #tablewrap {{ display: none; }}
  details.person {{ border: 1px solid #dfe3eb; border-left: 4px solid #9ad6b0;
                   border-radius: 8px; margin-bottom: 5px; background: #fff; }}
  details.person.flagged {{ border-left-color: #e3a93c; background: #fffdf7; }}
  details.person > summary {{ cursor: pointer; list-style: none; padding: 7px 11px;
                             display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  details.person > summary::-webkit-details-marker {{ display: none; }}
  .pid {{ font-size: 13px; min-width: 58px; }}
  .cols {{ display: flex; gap: 16px; flex-wrap: wrap; margin-left: auto; }}
  .cols > span {{ display: flex; align-items: baseline; gap: 5px; }}
  .cols em {{ font-style: normal; font-size: 9.5px; text-transform: uppercase;
             letter-spacing: .5px; color: #8b93a3; }}
  table.seasons {{ width: 100%; border-collapse: collapse; font-size: 11.5px; margin: 0 0 5px; }}
  table.seasons th {{ text-align: left; font-size: 9.5px; letter-spacing: .5px;
                     text-transform: uppercase; color: #8b93a3; font-weight: 600;
                     padding: 3px 11px; border-top: 1px solid #eceff4; }}
  table.seasons td {{ padding: 4px 11px; border-top: 1px solid #f2f4f8; }}
  td.sn {{ color: #8b93a3; font-weight: 700; width: 30px; }}
  td.state.complete {{ color: #176b35; }} td.state.partial {{ color: #7a5410; }}
  td.state.pending {{ color: #8a1f1f; }} td.state.empty {{ color: #8b93a3; }}
  .d {{ display: inline-flex; align-items: baseline; gap: 4px; font-size: 10.5px;
       text-transform: capitalize; padding: 0 6px; border-radius: 5px; margin-right: 3px;
       border: 1px solid #e0e4ec; }}
  .d i {{ font-style: normal; font-size: 9.5px; color: #6a7384; }}
  .d.complete {{ background: #eefaf2; border-color: #bfe6cd; }}
  .d.partial {{ background: #fdf6e6; border-color: #ecd6a2; }}
  .d.pending {{ background: #fdeeee; border-color: #ecc0c0; }}
  .d.none {{ background: #f5f6f9; color: #98a0ae; }}

  table.naming {{ width: calc(100% - 22px); margin: 2px 11px 8px;
                  border-collapse: collapse; font-size: 11px; }}
  table.naming th {{ text-align: left; font-size: 9.5px; letter-spacing: .5px;
                    text-transform: uppercase; color: #8b93a3; font-weight: 600;
                    padding: 3px 7px; background: #f7f8fb; }}
  table.naming td {{ padding: 4px 7px; border-top: 1px solid #f0f2f6; vertical-align: top; }}
  table.naming tr.skipped {{ background: #fdf5f5; }}
  table.naming tr.warning {{ background: #fffdf5; }}
  td.nsev {{ font-weight: 700; font-size: 9.5px; letter-spacing: .4px; white-space: nowrap; }}
  tr.skipped .nsev {{ color: #8a1f1f; }} tr.warning .nsev {{ color: #7a5410; }}
  td.nfile {{ font-family: ui-monospace, Consolas, monospace; font-size: 10px;
             word-break: break-all; }}
  .ninfo {{ margin: 0 11px 8px; font-size: 10.5px; color: #8b93a3; }}
  .nmark {{ margin-left: 4px; font-size: 9.5px; }}
  .nmark.skipped {{ color: #8a1f1f; }} .nmark.warning {{ color: #a9780f; }}

  table.plain {{ border-collapse: collapse; width: 100%; font-size: 11.5px; }}
  table.plain th, table.plain td {{ text-align: left; padding: 4px 8px;
                                   border-bottom: 1px solid #e6e9f0; }}
  table.plain th {{ background: #f4f6fa; font-weight: 600; color: #3b4354;
                   font-size: 10px; text-transform: uppercase; letter-spacing: .4px; }}
  .none {{ color: #5a6475; font-style: italic; }}
  .foot {{ margin-top: 24px; padding-top: 9px; border-top: 1px solid #e6e9f0;
          font-size: 10.5px; color: #5a6475; }}
  code {{ font-size: 10.5px; }}
  @media print {{
    body {{ padding: 0; font-size: 10px; }}
    .bar {{ display: none; }}
    table.grid th {{ position: static; }}
    details.person {{ page-break-inside: avoid; }}
  }}
</style>
<h1>{_e(report['title'])}</h1>
<div class="sub">{_e(report['generated_at'].replace('T', ' '))} ·
  {report['n_participants']} folder(s)
  {'· batch <code>' + _e(report['batch_id']) + '</code>' if report['batch_id'] else ''}
  · a picture of the data as it stands, not of any one run</div>

<div class="cards">
  <div class="card go"><b>{report['n_ready']}</b><span>ready</span></div>
  <div class="card stop"><b>{len(not_ready)}</b><span>not ready</span></div>
  <div class="card"><b>{report['n_data_complete']}</b><span>all devices</span></div>
  <div class="card"><b>{report['n_recordings']}</b><span>processed</span></div>
  <div class="card"><b>{v['PASS']}</b><span>pass</span></div>
  <div class="card"><b>{v['REVIEW']}</b><span>review</span></div>
  <div class="card"><b>{v['FAIL'] + v['ERROR']}</b><span>fail / error</span></div>
  <div class="card"><b>{'—' if report['pct_compliance_mean'] is None
                        else str(report['pct_compliance_mean']) + '%'}</b>
    <span>mean compliance</span></div>
</div>

<div class="bar">
  <button id="b-table" class="on">Table</button>
  <button id="b-detail">Detail</button>
  <input id="q" type="search" placeholder="filter folders…" autocomplete="off">
  <button id="b-flagged">Not ready only</button>
  <span class="hint">click any heading to sort · sorted by <b>has data</b>, then ready, then folder</span>
</div>

<div id="tablewrap">{_table_view(people)}</div>

<div id="detail">
{blocks or '<p class="none">Nothing selected.</p>'}
</div>

<h2>Naming check — every problem, worst first</h2>
{_table(['', 'folder', 'season', 'file', 'what is wrong'],
        [[f'<b class="nmark {_e(str(f.get("severity") or ""))}">'
          f'{"\u2715" if f.get("severity") == "skipped" else "\u26a0"}</b> '
          + _e(SEV_WORD.get(str(f.get("severity")), str(f.get("severity") or ""))),
          str(f.get("participant") or ""),
          str(f.get("season") or "\u2014"),
          f'<code>{_e(str(f.get("file") or f.get("name") or ""))}</code>',
          str(f.get("message") or "")]
         for f in sorted(
             (f for f in report["naming"]["flags"] if f.get("severity") != "info"),
             key=lambda f: (_SEV_RANK.get(str(f.get("severity")), 9),
                            str(f.get("participant") or ""), str(f.get("season") or "")))],
        "plain")
 if any(f.get("severity") != "info" for f in report["naming"]["flags"])
 else '<p class="none">No file is unreadable and no filename drifts from the convention.</p>'}
<p class="why"><b>NOT READ</b> means the file is never opened, so it gets no verdict
and the recording counts as missing. <b>warning</b> means it is read and processed,
and the name should still be corrected.
{(' ' + str(flag_counts.get('info', 0)) + ' further cosmetic note(s) are not listed.')
 if flag_counts.get('info') else ''}</p>

<div class="foot">
  Staging: <code>{_e(report['source_root'])}</code><br>
  Pre-processed: <code>{_e(report['output_root'])}</code><br>
  <b>Ready</b> means every expected device has data and no file is being skipped —
  it does not mean the data is compliant, which is what the verdicts say.<br>
  Contains participant identifiers — keep it inside Tier 1.
</div>

<script>
(function () {{
  var body = document.body, table = document.getElementById("ptable");
  function view(detail) {{
    body.classList.toggle("detail", detail);
    document.getElementById("b-table").classList.toggle("on", !detail);
    document.getElementById("b-detail").classList.toggle("on", detail);
  }}
  document.getElementById("b-table").onclick = function () {{ view(false); }};
  document.getElementById("b-detail").onclick = function () {{ view(true); }};

  if (!table) return;
  var tbody = table.tBodies[0];
  var rows = [].slice.call(tbody.rows);

  // Sort on the value stashed in data-v, so a chip sorts by its number and a
  // state by its word rather than by the markup that draws it.
  var sorted = null, asc = true;
  [].forEach.call(table.tHead.rows[0].cells, function (th, i) {{
    th.onclick = function () {{
      asc = sorted === i ? !asc : true;
      sorted = i;
      [].forEach.call(table.tHead.rows[0].cells, function (o) {{
        o.classList.remove("sorted", "up");
      }});
      th.classList.add("sorted");
      if (asc) th.classList.add("up");
      var num = th.dataset.kind === "num";
      rows.sort(function (a, b) {{
        var x = a.cells[i].dataset.v, y = b.cells[i].dataset.v;
        var d = num ? (parseFloat(x) || 0) - (parseFloat(y) || 0)
                    : String(x).localeCompare(String(y));
        return asc ? d : -d;
      }});
      rows.forEach(function (r) {{ tbody.appendChild(r); }});
    }};
  }});

  var q = document.getElementById("q"), only = false;
  function apply() {{
    var needle = q.value.trim().toLowerCase();
    rows.forEach(function (r) {{
      var hit = !needle || r.textContent.toLowerCase().indexOf(needle) >= 0;
      if (only && !r.classList.contains("flagged")) hit = false;
      r.classList.toggle("hide", !hit);
    }});
  }}
  q.oninput = apply;
  document.getElementById("b-flagged").onclick = function () {{
    only = !only;
    this.classList.toggle("on", only);
    apply();
  }};
}})();
</script>
"""


# ----------------------------------------------------------------------- XLSX


#: Fills used across the workbook. Named once so the Key sheet cannot drift
#: out of step with the sheets it explains.
FILLS = {
    "good": ("C6EFCE", "0B6B32", "all present / all done / passed"),
    "warn": ("FFEB9C", "7A5410", "partly there / needs review"),
    "bad": ("FFC7CE", "9C0006", "missing / not done / failed"),
    "none": ("EDEFF3", "5A6475", "nothing to judge"),
}


def write_xlsx(report: dict[str, Any], path: Path) -> Optional[Path]:
    """A workbook of the same report, colour-coded. None if openpyxl is missing."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:                                   # pragma: no cover
        return None

    wb = Workbook()
    head_font = Font(bold=True, color="1B1F28")
    head_fill = PatternFill("solid", fgColor="E7EBF2")
    title_font = Font(bold=True, size=13)
    muted = Font(color="5A6475", size=10)

    def fill(key: str) -> "PatternFill":
        return PatternFill("solid", fgColor=FILLS[key][0])

    def font(key: str) -> "Font":
        return Font(color=FILLS[key][1], bold=True)

    first = [True]

    def sheet(name: str, headers: list[str], rows: list[list[Any]],
              note: str = "", colours: Optional[dict[int, list[str]]] = None):
        """One sheet. `colours` maps a 1-based column -> a key per row."""
        ws = wb.active if first[0] else wb.create_sheet()
        first[0] = False
        ws.title = name
        top = 1
        if note:
            ws.cell(row=1, column=1, value=note).font = muted
            top = 3
        ws.append([]) if note else None
        ws.cell(row=top, column=1)
        for i, h in enumerate(headers, start=1):
            c = ws.cell(row=top, column=i, value=h)
            c.font, c.fill = head_font, head_fill
            c.alignment = Alignment(vertical="center", wrap_text=True)
        for j, row in enumerate(rows):
            for i, value in enumerate(row, start=1):
                c = ws.cell(row=top + 1 + j, column=i, value=value)
                key = (colours or {}).get(i, [None] * len(rows))[j] if colours else None
                if key:
                    c.fill, c.font = fill(key), font(key)
        for i, h in enumerate(headers, start=1):
            longest = max([len(str(h))] + [len(str(r[i - 1])) for r in rows] or [0])
            ws.column_dimensions[get_column_letter(i)].width = min(46, max(10, longest + 3))
        ws.freeze_panes = ws.cell(row=top + 1, column=1)
        return ws

    # --- Key -----------------------------------------------------------------
    ws = wb.active
    first[0] = False
    ws.title = "Key"
    ws["A1"] = report["title"]
    ws["A1"].font = title_font
    ws["A2"] = f"Generated {report['generated_at'].replace('T', ' ')}"
    ws["A2"].font = muted
    rows = [
        ("", ""),
        ("What this workbook answers", ""),
        ("Ready", "every expected device has data AND no file is being skipped. "
                  "It does NOT mean the data is compliant - that is the verdict."),
        ("Data", "how many of the expected devices have any data at all."),
        ("Processing", "of the data that is here, how much has been through the pipeline."),
        ("Verdict", "of what was processed, did it meet the compliance thresholds."),
        ("Naming", "blocking = the file is not read at all, so it never gets a verdict. "
                   "warning = read, but the filename drifts from the convention."),
        ("", ""),
        ("Colour", ""),
    ]
    for i, (a, b) in enumerate(rows, start=4):
        ws.cell(row=i, column=1, value=a).font = Font(bold=bool(a) and not b)
        ws.cell(row=i, column=2, value=b).alignment = Alignment(wrap_text=True)
    r = 4 + len(rows)
    for key, (_bg, _fg, meaning) in FILLS.items():
        c = ws.cell(row=r, column=1, value=key)
        c.fill, c.font = fill(key), font(key)
        ws.cell(row=r, column=2, value=meaning)
        r += 1
    ws.cell(row=r + 1, column=1, value="Staging").font = Font(bold=True)
    ws.cell(row=r + 1, column=2, value=report["source_root"])
    ws.cell(row=r + 2, column=1, value="Pre-processed").font = Font(bold=True)
    ws.cell(row=r + 2, column=2, value=report["output_root"])
    ws.cell(row=r + 4, column=1,
            value="Contains participant identifiers - keep inside Tier 1.").font = muted
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 96

    # --- Participants: the decision sheet ------------------------------------
    people = sorted(report["people"], key=lambda x: (x["ready"], x["participant"]))
    prows, ready_c, data_c, proc_c, name_c = [], [], [], [], []
    for x in people:
        v = x["verdicts"]
        prows.append([
            x["participant"],
            "READY" if x["ready"] else "not ready",
            "; ".join(x["why_not_ready"]) or "",
            f'{len(x["devices_present"])}/{x["n_devices_expected"]}',
            ", ".join(x["devices_missing"]) or "none",
            x["seasons"],
            x["processing"], x["done"], x["items"], x["todo"],
            v["PASS"], v["REVIEW"], v["FAIL"] + v["ERROR"],
            x["pct_compliance_mean"],
            x["blocking_flags"], x["naming_warnings"],
        ])
        ready_c.append("good" if x["ready"] else "bad")
        data_c.append({"full": "good", "partial": "warn"}.get(x["data_state"], "bad"))
        proc_c.append({"complete": "good", "partial": "warn",
                       "pending": "bad"}.get(x["processing"], "none"))
        name_c.append("bad" if x["blocking_flags"] else
                      "warn" if x["naming_warnings"] else "good")
    sheet("Participants",
          ["folder", "ready to process", "why not", "devices with data", "missing",
           "seasons", "processing", "done", "items", "to do",
           "pass", "review", "fail", "mean %compliance",
           "blocking naming", "naming warnings"],
          prows,
          note="One row per participant. Sorted so anything not ready is at the top.",
          colours={2: ready_c, 4: data_c, 7: proc_c, 15: name_c})

    # --- Season x device ------------------------------------------------------
    srows, scol, dcol = [], [], []
    for x in people:
        for sn in x["season_rows"]:
            for d in sn["devices"]:
                srows.append([x["participant"], sn["n"], sn["label"], sn["state"],
                              d["device"], d["state"], d["done"], d["of"],
                              ", ".join(d["verdicts"]) or ""])
                scol.append({"complete": "good", "partial": "warn",
                             "pending": "bad"}.get(sn["state"], "none"))
                dcol.append({"complete": "good", "partial": "warn",
                             "pending": "bad"}.get(d["state"], "none"))
    sheet("Season x device",
          ["folder", "season no", "season", "season state", "device", "device state",
           "processed", "found", "verdicts"], srows,
          note="Every participant-season-device the pipeline knows about.",
          colours={4: scol, 6: dcol})

    # --- Recordings -----------------------------------------------------------
    rrows, vcol = [], []
    for r_ in report["rows"]:
        verdict = (r_.get("verdict") or "").upper()
        rrows.append([r_["participant"], r_["season"], r_["device"], r_.get("verdict"),
                      r_.get("valid_days"), r_.get("total_days"),
                      r_.get("pct_compliance_mean"), r_.get("stem")])
        vcol.append({"PASS": "good", "REVIEW": "warn",
                     "FAIL": "bad", "ERROR": "bad"}.get(verdict, "none"))
    sheet("Recordings",
          ["folder", "season", "device", "verdict", "valid days", "total days",
           "mean %compliance", "file"], rrows,
          note="Only recordings that were processed. A skipped file never appears here.",
          colours={4: vcol})

    # --- Outstanding + naming -------------------------------------------------
    sheet("Outstanding",
          ["folder", "season no", "season", "device", "state", "done", "found"],
          [[o["participant"], o["n"], o["season"], o["device"], o["state"],
            o["done"], o["of"]] for o in report["outstanding"]],
          note="Has input, not finished. These are what a run would work on.")
    nrows, ncol = [], []
    for f in report["naming"]["flags"]:
        sev = str(f.get("severity") or "info")
        nrows.append([sev, f.get("participant"), f.get("file") or f.get("name"),
                      f.get("message")])
        ncol.append({"skipped": "bad", "warning": "warn"}.get(sev, "none"))
    sheet("Naming", ["severity", "folder", "file", "what is wrong"], nrows,
          note='"skipped" blocks processing. "warning" and "info" do not.',
          colours={1: ncol})

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


# ------------------------------------------------------------------- writing


def slug(text: str) -> str:
    keep = [c if c.isalnum() or c in "-_" else "-" for c in str(text)]
    return "".join(keep).strip("-")[:60] or "report"


def save(config: Config, report: dict[str, Any]) -> dict[str, Any]:
    """Write the HTML, the workbook and the raw JSON, and say where they went."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"compliance-{slug(report['batch_id'] or stamp)}"
    base = reports_dir(config)
    base.mkdir(parents=True, exist_ok=True)

    html_path = base / f"{name}.html"
    html_path.write_text(render_html(report), encoding="utf-8")
    json_path = base / f"{name}.json"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    xlsx_path = write_xlsx(report, base / f"{name}.xlsx")

    return {"name": name, "dir": str(base), "html": str(html_path),
            "json": str(json_path), "xlsx": str(xlsx_path) if xlsx_path else None}
