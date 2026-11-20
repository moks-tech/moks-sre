"""Unified SRE intelligence: one investigation pipeline over every source MOKS can read (docs/UNIFIED_INVESTIGATION.md).

    detect -> classify -> identify -> telemetry -> traces -> changes -> anomalies -> history -> semantic
           -> hypotheses -> more_evidence -> validate -> rca -> remediation -> report

Code collects the evidence, through the same ToolExecutor every MOKS read passes (RBAC, scope, redaction, audit),
and turns each stored result into one shared record, the evidence contract (evidence.py). CrewAI agents reason over
those records: an analyst proposes hypotheses and asks for more evidence from an allowlist, a reviewer challenges
them without seeing the analyst's reasoning (agents.py). Code decides what stands: every citation is checked against
the ledger, hypotheses are scored by named factors (scoring.py), and the reviewer can lower a score, never raise it.

The pipeline runs as the CrewAI console flow `unified_investigation`, on the CrewAI worker. Nothing here imports
crewai at module import; without crewai or a model the reasoning stages are rule-based and the report says so.
"""
from __future__ import annotations

ENGINE = "unified"
FLOW = "unified_investigation"  # its CrewAI console flow (crew/config.py FLOWS)
ENGINE_VERSION = "1"
