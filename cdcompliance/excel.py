"""Excel workbook generation.

Two artifacts:

1. Per-item workbook (``<bin_stem>_compliance.xlsx``) written into the item's
   output ``Actigraph`` folder — a Summary sheet, the compliance per-day table,
   and each Step 2 metrics CSV as its own sheet.

2. A participant-level roll-up: one row per processed item, maintained as a CSV
   master (append-safe across runs) and re-rendered to ``.xlsx``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pandas as pd

from .models import ComplianceResult


def _write_kv_sheet(writer: pd.ExcelWriter, sheet: str, mapping: dict[str, Any]) -> None:
    rows = [{"Field": k, "Value": _flatten(v)} for k, v in mapping.items()]
    pd.DataFrame(rows).to_excel(writer, sheet_name=sheet, index=False)


def _flatten(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return str(value)
    return value


def build_item_workbook(
    out_xlsx: Path,
    context: dict[str, Any],
    compliance: Optional[ComplianceResult],
    step2_files: dict[str, Path],
) -> Path:
    out_xlsx = Path(out_xlsx)
    out_xlsx.parent.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = dict(context)
    if compliance is not None:
        summary["compliance_verdict"] = compliance.verdict
        for k, v in compliance.summary.items():
            summary[f"compliance_{k}"] = v
        for k, v in compliance.parameters.items():
            summary[f"param_{k}"] = v

    with pd.ExcelWriter(out_xlsx, engine="openpyxl") as writer:
        _write_kv_sheet(writer, "Summary", summary)

        if compliance is not None and compliance.per_day:
            pd.DataFrame(compliance.per_day).to_excel(
                writer, sheet_name="Compliance_PerDay", index=False
            )

        # One sheet per Step 2 CSV output.
        sheet_names = {
            "nonparametric": "Step2_Nonparametric",
            "daily": "Step2_Daily",
            "periodogram": "Step2_Periodogram",
            "sri": "Step2_SRI",
        }
        for key, sheet in sheet_names.items():
            path = step2_files.get(key)
            if path and Path(path).exists():
                try:
                    df = pd.read_csv(path)
                    df.to_excel(writer, sheet_name=sheet[:31], index=False)
                except Exception as exc:  # never let one bad CSV kill the workbook
                    pd.DataFrame([{"error": str(exc), "file": str(path)}]).to_excel(
                        writer, sheet_name=sheet[:31], index=False
                    )
    return out_xlsx


def append_participant_summary(
    summary_csv: Path, row: dict[str, Any]
) -> tuple[Path, Path]:
    """Append a row to the participant master CSV and re-render an XLSX copy.

    Returns (csv_path, xlsx_path).
    """
    summary_csv = Path(summary_csv)
    summary_csv.parent.mkdir(parents=True, exist_ok=True)

    if summary_csv.exists():
        existing = pd.read_csv(summary_csv)
        combined = pd.concat([existing, pd.DataFrame([row])], ignore_index=True)
    else:
        combined = pd.DataFrame([row])

    # De-duplicate on the natural key, keeping the latest row.
    key_cols = [c for c in ("participant", "season", "device", "input_file") if c in combined.columns]
    if key_cols:
        combined = combined.drop_duplicates(subset=key_cols, keep="last")

    combined.to_csv(summary_csv, index=False)
    xlsx_path = summary_csv.with_suffix(".xlsx")
    try:
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            combined.to_excel(writer, sheet_name="Compliance_Summary", index=False)
    except Exception:
        xlsx_path = summary_csv  # xlsx optional; CSV is the source of truth
    return summary_csv, xlsx_path
