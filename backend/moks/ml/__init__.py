"""Machine learning in MOKS: assistance that is measured, switchable and never in charge.

  learning.py    versioned learning records, operator judgments, reviewed labels, datasets without leakage
  similar.py     similar historical incidents over approved postmortems, through the existing RAG search
  features.py    feature catalog and Prometheus extraction on one fixed grid: sources pinned per cohort, sampling
                 probe, zero kept apart from missing, provenance per feature; workloads and nodes
  detectors.py   seven detectors behind one interface (statistical, rolling, EWMA, seasonal, decomposition,
                 Isolation Forest, LOF), JSON state, held-out thresholds, explanations
  baseline.py    the hour-of-week median and MAD the seasonal detector uses
  iforest.py     a small Isolation Forest whose artifact is JSON, so loading it runs no code
  artifacts.py   artifact storage with checksum, schema and library-version validation
  registry.py    model versions: candidate, shadow, champion, retired; promotion and rollback
  anomaly.py     training and comparison (offline Job), bounded shadow inference, health, reproduction
  benchmark.py   synthetic incidents with known answers, injected faults, event-based metrics
  correlate.py   groups anomalies into one probable problem with unvalidated candidate explanations
  history.py     recurring failures, infrastructure problems, runbooks and what worked, from reviewed incidents
  signals.py     a target's anomalies and groups as investigation evidence (kind model_output)
  skill_rank.py  semantic ranking over skills already eligible by policy
  evaluation.py  retrieval and detection metrics, chronological and grouped splits

Every capability has a mode in ml_capabilities: disabled, shadow (computed, shown, judged, and nothing else) or
enabled. MOKS_ML_DISABLED forces all of them off. A mode is read on every use, so switching one to disabled is the
kill switch, and each capability falls back to what MOKS did without it. Nothing here grants a permission,
creates an incident, changes a severity or runs a remediation.
"""
from __future__ import annotations

import logging
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import MLCapability

log = logging.getLogger("moks.ml")

Mode = Literal["disabled", "shadow", "enabled"]
CAPABILITIES: dict[str, dict] = {
    "similar_incidents": {"default": "enabled", "modes": ("disabled", "shadow", "enabled"),
                          "describe": "Similar historical incidents in investigations. Read-only retrieval over approved postmortems."},
    "anomaly_detection": {"default": "disabled", "modes": ("disabled", "shadow"),
                          "describe": "Unusual metric behavior per workload and node, grouped into correlated problems and offered to investigations as leads. "
                                      "Shadow only: shown and judged; no incident, severity or action follows."},
    "skill_recommendations": {"default": "disabled", "modes": ("disabled", "shadow", "enabled"),
                              "describe": "Semantic ranking of skills already eligible by policy. Shadow records it; enabled may add a skill the rules did not pick, never remove one."},
}


async def mode(db: AsyncSession, name: str) -> Mode:
    """The capability's mode now. Unknown or unreadable means disabled: a broken switch must fail closed."""
    if get_settings().ml_disabled or name not in CAPABILITIES:
        return "disabled"
    try:
        row = (await db.execute(select(MLCapability).where(MLCapability.name == name))).scalar_one_or_none()
    except Exception:
        log.warning("capability mode unreadable", extra={"event": "ml_mode_unreadable", "operation": name})
        return "disabled"
    value = row.mode if row else CAPABILITIES[name]["default"]
    return value if value in CAPABILITIES[name]["modes"] else "disabled"  # type: ignore[return-value]


async def modes(db: AsyncSession) -> dict[str, dict]:
    rows = {r.name: r for r in (await db.execute(select(MLCapability))).scalars().all()}
    forced = get_settings().ml_disabled
    out = {}
    for name, spec in CAPABILITIES.items():
        r = rows.get(name)
        out[name] = {"mode": "disabled" if forced else (r.mode if r else spec["default"]), "allowed": list(spec["modes"]), "describe": spec["describe"],
                     "reason": "MOKS_ML_DISABLED is set" if forced else (r.reason if r else "default"), "updated_at": r.updated_at.isoformat() if r and r.updated_at else None,
                     "forced_off": forced}
    return out
