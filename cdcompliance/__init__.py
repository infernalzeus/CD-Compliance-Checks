"""CD-Compliance-Checks — CHiP-D data compliance pipeline.

A modular, config-driven pipeline that runs quality/compliance checks on
participant device data held in a OneDrive-synced repository and replicates the
processed, checked outputs into a dashboard output tree.

The package is intentionally split into a reusable *core* (this package) and a
thin CLI (`run.py`). Every long-running operation emits structured events via
`cdcompliance.events`, so a future web UI can drive the same core and stream
progress (OneDrive download %, per-step status, compliance verdicts) without any
changes to the pipeline logic.

Currently implemented device: **Actigraph** (GENEActiv `.bin`).
Stubbed for later: Expiwell, Saliva, Cognitron, Qualtrics, MiEye.
"""

__version__ = "0.1.0"
