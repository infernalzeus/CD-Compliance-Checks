"""The charts that go with a T2 export, as a PDF.

Deliberately the same content as the T2 preview, in the same order, so the PDF
is a record of what was on screen rather than a second, differently-computed
story. It is built from the already-filtered export tables, so the day range and
cut-offs in force are the ones drawn.

matplotlib is imported lazily: an installation without it still exports the
data, and says the PDF was skipped rather than failing the whole export.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

#: Charts stay legible, so a page holds at most this many small panels.
PANELS_PER_PAGE = 9
#: Timelines are per participant-season; beyond this many, the PDF only
#: summarises (a 200-page appendix helps nobody).
MAX_TIMELINE_PAGES = 12
INK = "#1a1a1a"
GREY = "#6b7280"
DEVICE_COLOUR = {"actigraph": "#2f6fd0", "mieye": "#c98a12", "expiwell": "#7b4fd0"}


def available() -> bool:
    try:
        import matplotlib  # noqa: F401
        return True
    except ImportError:
        return False


def _spread(measures: list[str], devices: dict[str, str]) -> list[str]:
    """Order measures device by device, so one page covers the whole study."""
    queues: dict[str, list[str]] = {}
    for m in measures:
        queues.setdefault(devices.get(m, ""), []).append(m)
    order, i = [], 0
    while len(order) < len(measures):
        for q in queues.values():
            if i < len(q):
                order.append(q[i])
        i += 1
    return order


def _fig_text(fig, x, y, text, size=9, colour=INK, weight="normal", ha="left"):
    fig.text(x, y, text, size=size, color=colour, weight=weight, ha=ha, va="top", wrap=True)


def _cover(pdf, plt, manifest: dict[str, Any], inclusion: dict[str, Any]) -> None:
    fig = plt.figure(figsize=(8.27, 11.69))          # A4 portrait
    _fig_text(fig, 0.08, 0.94, "T2 dataset", size=26, weight="bold")
    _fig_text(fig, 0.08, 0.89, manifest.get("name") or manifest.get("selection_id", ""),
              size=13, colour=GREY)
    rows = [
        ("Export", manifest.get("export_id", "")),
        ("From selection", manifest.get("selection_id", "")),
        ("Approved by", manifest.get("approved_by", "")),
        ("Exported", (manifest.get("exported_at") or "").replace("T", " ")),
        ("", ""),
        ("Participants", inclusion.get("participants")),
        ("Participant-seasons", inclusion.get("participant_seasons")),
        ("Seasons", ", ".join(inclusion.get("seasons") or []) or "—"),
        ("Devices", ", ".join(inclusion.get("devices") or [])),
        ("Days", inclusion.get("days")),
        ("Measures", inclusion.get("measures")),
        ("Day range", (f"days {inclusion['day_range'][0]}–{inclusion['day_range'][1]} of each shared window"
                       if inclusion.get("day_range") else "all days")),
        ("Part-days", inclusion.get("part_days")),
        ("Cut-offs", " · ".join(f"{c['variable']} {c['op']} {c['value']}"
                                for c in (inclusion.get("cut_offs") or [])) or "none"),
    ]
    y = 0.80
    for label, value in rows:
        if label:
            _fig_text(fig, 0.08, y, label, size=9, colour=GREY)
            _fig_text(fig, 0.34, y, str(value), size=10)
        y -= 0.028
    _fig_text(fig, 0.08, y - 0.02,
              "Every day in this dataset had already passed the compliance stage before it "
              "reached T2. The charts here are exploratory: they show what the data looks "
              "like, they do not test a pre-registered hypothesis.", size=8.5, colour=GREY)
    pdf.savefig(fig)
    plt.close(fig)


def _coverage_page(pdf, plt, long: pd.DataFrame) -> None:
    day = long[long["level"] == "day"]
    if day.empty:
        return
    spans = (day.groupby(["id", "season", "device"])["date"]
                .agg(["min", "max", "count"]).reset_index())
    rows = spans.groupby(["id", "season"], sort=True)
    labels, bars = [], []
    for (pid, season), g in rows:
        labels.append(f"{pid}\n{season}")
        bars.append(g)
    height = max(3.0, 0.7 * len(labels) + 1.6)
    fig, ax = plt.subplots(figsize=(8.27, min(11.69, height)))
    seen_devices = []
    for i, g in enumerate(bars):
        for _, r in g.iterrows():
            start, end = pd.Timestamp(r["min"]), pd.Timestamp(r["max"])
            dev = r["device"]
            if dev not in seen_devices:
                seen_devices.append(dev)
            offset = (seen_devices.index(dev) - 1) * 0.22
            ax.barh(i + offset, (end - start).days + 1, left=start, height=0.2,
                    color=DEVICE_COLOUR.get(dev, GREY), label=dev if i == 0 else None)
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels, fontsize=8)
    ax.invert_yaxis()
    ax.set_title("What was recorded, and when", fontsize=12, loc="left")
    ax.tick_params(axis="x", labelsize=8, rotation=30)
    handles = [plt.Line2D([0], [0], color=DEVICE_COLOUR.get(d, GREY), lw=6) for d in seen_devices]
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.tight_layout(rect=[0, 0.10, 1, 1])
    fig.legend(handles, seen_devices, fontsize=8, frameon=False, ncol=len(seen_devices),
               loc="lower center", bbox_to_anchor=(0.5, 0.02))
    pdf.savefig(fig)
    plt.close(fig)


def _season_pages(pdf, plt, long: pd.DataFrame, units: dict[str, str],
                  devices: Optional[dict[str, str]] = None) -> None:
    """Each measure by season - or, when only one season is here, by person.

    A "by season" page for a single season is a row of lone bars; comparing the
    participants at least says something.
    """
    day = long[long["level"] == "day"]
    if day.empty:
        return
    by_season = day["season"].nunique() > 1
    key = "season" if by_season else "id"
    title = ("Each measure, by season of the year" if by_season
             else "Each measure, participant by participant")
    footer = ("Bars are the mean across participants (each participant counted once, whatever "
              "their number of days); whiskers are 95 % intervals."
              if by_season else
              "One season here, so each bar is a participant's own mean; whiskers are 95 % "
              "intervals across their days.")
    per_person = (day.groupby(["variable", key, "id"])["value"].mean().reset_index()
                  if by_season else day.groupby(["variable", key])["value"]
                  .agg(["mean", "std", "count"]).reset_index())
    measures = _spread(sorted(day["variable"].unique()), devices or {})
    for start in range(0, len(measures), PANELS_PER_PAGE):
        chunk = measures[start:start + PANELS_PER_PAGE]
        fig, axes = plt.subplots(3, 3, figsize=(8.27, 11.69))
        fig.suptitle(title, fontsize=12, x=0.08, ha="left")
        for ax, measure in zip(axes.flat, chunk):
            g = per_person[per_person["variable"] == measure]
            stats = (g.groupby(key)["value"].agg(["mean", "std", "count"]) if by_season
                     else g.set_index(key)[["mean", "std", "count"]])
            ax.bar(range(len(stats)), stats["mean"], color="#2f6fd0", alpha=.75,
                   yerr=(1.96 * stats["std"] / stats["count"].pow(.5)).fillna(0),
                   capsize=3, error_kw={"elinewidth": 1, "ecolor": INK})
            ax.set_xticks(range(len(stats)))
            ax.set_xticklabels([str(s).replace(" ", "\n") for s in stats.index], fontsize=6.5)
            ax.set_title(measure, fontsize=8, loc="left")
            ax.set_ylabel(units.get(measure, ""), fontsize=6.5)
            ax.tick_params(axis="y", labelsize=6.5)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
        for ax in list(axes.flat)[len(chunk):]:
            ax.axis("off")
        fig.text(0.08, 0.035, footer, size=7.5, color=GREY)
        fig.tight_layout(rect=[0, 0.05, 1, 0.96])
        pdf.savefig(fig)
        plt.close(fig)


def _timeline_pages(pdf, plt, long: pd.DataFrame, units: dict[str, str],
                    devices: Optional[dict[str, str]] = None) -> int:
    """One page per participant-season: every measure over the days."""
    day = long[long["level"] == "day"]
    if day.empty:
        return 0
    pages = 0
    for (pid, season), g in day.groupby(["id", "season"], sort=True):
        if pages >= MAX_TIMELINE_PAGES:
            break
        measures = _spread(sorted(g["variable"].unique()), devices or {})[:PANELS_PER_PAGE]
        if not measures:
            continue
        fig, axes = plt.subplots(3, 3, figsize=(8.27, 11.69))
        fig.suptitle(f"{pid} · {season} — day by day", fontsize=12, x=0.08, ha="left")
        for ax, measure in zip(axes.flat, measures):
            m = g[g["variable"] == measure].sort_values("date")
            ax.plot(pd.to_datetime(m["date"]), m["value"], marker="o", ms=2.5, lw=1,
                    color=DEVICE_COLOUR.get(m["device"].iloc[0], GREY))
            ax.set_title(measure, fontsize=8, loc="left")
            ax.set_ylabel(units.get(measure, ""), fontsize=6.5)
            ax.tick_params(axis="both", labelsize=6, rotation=0)
            for label in ax.get_xticklabels():
                label.set_rotation(30)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
        for ax in list(axes.flat)[len(measures):]:
            ax.axis("off")
        fig.tight_layout(rect=[0, 0.02, 1, 0.96])
        pdf.savefig(fig)
        plt.close(fig)
        pages += 1
    return pages


def _agreement_page(pdf, plt, long: pd.DataFrame, pairs: list[tuple[str, str]]) -> None:
    """Clock-hour measures that should land together, day by day."""
    from . import t2_stats

    day = long[long["level"] == "day"]
    drawn = []
    for x, y in pairs:
        gx = day[day["variable"] == x][["id", "season", "date", "value"]].rename(columns={"value": "x"})
        gy = day[day["variable"] == y][["id", "season", "date", "value"]].rename(columns={"value": "y"})
        merged = gx.merge(gy, on=["id", "season", "date"], how="inner").dropna()
        if len(merged) >= 4:
            drawn.append((x, y, merged))
    if not drawn:
        return
    fig, axes = plt.subplots(len(drawn), 1, figsize=(8.27, min(11.69, 3.6 * len(drawn) + 1)))
    axes = [axes] if len(drawn) == 1 else list(axes)
    fig.suptitle("Do these land on the same hour?", fontsize=12, x=0.08, ha="left")
    for ax, (x, y, merged) in zip(axes, drawn):
        diffs = [t2_stats.circular_difference(a, b) for a, b in zip(merged["x"], merged["y"])]
        within = sum(1 for d in diffs if abs(d) <= 1)
        ax.hist(diffs, bins=range(-12, 13), color="#2f6fd0", alpha=.8)
        ax.axvline(0, color=INK, lw=1)
        ax.set_title(f"{x} − {y}: {within} of {len(diffs)} days within ±1 h", fontsize=9, loc="left")
        ax.set_xlabel("hours apart (negative = the first is earlier)", fontsize=7.5)
        ax.set_ylabel("days", fontsize=7.5)
        ax.tick_params(labelsize=7)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    fig.tight_layout(rect=[0, 0.02, 1, 0.95])
    pdf.savefig(fig)
    plt.close(fig)


def build_pdf(path: Path, long: pd.DataFrame, dictionary_rows: list[dict[str, Any]],
              manifest: dict[str, Any], inclusion: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Write the charts PDF. Returns a small summary, or None if it was skipped."""
    if not available() or long.empty:
        return None
    import matplotlib
    matplotlib.use("Agg")                     # no display on a lab machine or a server
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    units = {r["measure"]: r.get("unit", "") for r in dictionary_rows}
    devices = {r["measure"]: r.get("device", "") for r in dictionary_rows}
    clock = [r["measure"] for r in dictionary_rows if r.get("unit") == "clock hour"]
    present = set(long["variable"].unique())
    pairs = [(a, b) for i, a in enumerate(clock) for b in clock[i + 1:]
             if a in present and b in present][:3]

    with PdfPages(path) as pdf:
        _cover(pdf, plt, manifest, inclusion)
        _coverage_page(pdf, plt, long)
        _season_pages(pdf, plt, long, units, devices)
        timelines = _timeline_pages(pdf, plt, long, units, devices)
        _agreement_page(pdf, plt, long, pairs)
        info = pdf.infodict()
        info["Title"] = f"T2 dataset {manifest.get('export_id', '')}"
        info["Subject"] = "CHiP-D T2 export - exploratory charts"
        info["CreationDate"] = datetime.now()
        pages = pdf.get_pagecount()
    return {"pages": pages, "timeline_pages": timelines,
            "timelines_truncated": timelines >= MAX_TIMELINE_PAGES}
