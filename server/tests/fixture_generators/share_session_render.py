"""Deterministic normalization for the real Share session producer."""

from __future__ import annotations

import json
from typing import Any


def normalize_envelope(body: dict[str, Any], chain: Any) -> dict[str, Any]:
    """Normalize only identities and timestamps minted by the real producer."""
    replacements = {
        chain.result_id: "qr_FIXTURE",
        chain.render_id: "rnd_FIXTURE",
        chain.spec_version_id: "vsv_FIXTURE",
        chain.project_id: "proj_FIXTURE",
        chain.org_id: "org_FIXTURE",
    }
    if chain.ai_path_id:
        replacements[chain.ai_path_id] = "aip_FIXTURE"
    raw = json.dumps(body, sort_keys=True)
    for actual, stable in replacements.items():
        raw = raw.replace(actual, stable)
    normalized = json.loads(raw)
    disclosure = normalized.get("disclosure") or {}
    for key in ("shared_on", "expires_at"):
        if key in disclosure:
            disclosure[key] = "FIXTURE_TIMESTAMP"
    for fact in normalized.get("facts") or []:
        if fact.get("label") == "Shared on":
            fact["value"] = "FIXTURE_DATE"
    evidence = normalized.get("ai_path_evidence") or {}
    for step in evidence.get("steps") or []:
        if "observed_at" in step:
            step["observed_at"] = "2026-01-01T00:00:00Z"
    feedback = normalized.get("feedback_context")
    if isinstance(feedback, dict):
        normalized["feedback_context"] = {
            "schema_version": feedback.get("schema_version"),
            "token": "FIXTURE_SIGNED_CONTEXT",
            "interaction_ref": "afi_FIXTURE",
            "expires_at": "2026-01-01T00:15:00Z",
        }
    return normalized
