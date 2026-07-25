"""Published-insight contract for Epic 35 (agentic daily insights).

Story 35.0 (spike / hard gate). This module is the SINGLE contract shared by the
research agent, the toorow server and the existing renderer. It introduces NO new
render primitive: card composition reuses the block-type vocabulary and template ids
already defined in ``server/core/cards.py`` (Epic 23 dataviz).

Two things live here:

1. ``SCHEMA_VERSION`` + ``PUBLISHED_INSIGHT_SCHEMA`` — the JSON shape of a published
   insight payload (editorial insight + analyzed period + card contract + refs).
2. ``validate_published_insight(...)`` — a PURE, fail-closed validator that enforces
   the Epic 35 §8 gate order and returns a stable ``reason_code`` on the first failure.
   No DB / warehouse calls: every server-resolved input (available metrics/dimensions,
   resolvable evidence, freshness, project access, existing slots, expected hash) is
   passed in by the caller. The persistence + MCP wiring is Stories 35.2 / 35.3.

Design note (A vs B gate, Story 35.0): ``card.mode == "template"`` is the ratified
A path (select an existing card by keys). ``card.mode == "compose"`` (B, declarative
composition) is reserved and validator-gated but DISABLED by default — it only clears
the gate if a live spike proves it materially outperforms A (Epic 35 §10). Enabling B
later needs ``allow_compose=True``, not a schema rewrite.

ASCII-only module (L-3).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date

# ---------------------------------------------------------------------------
# Versioning + vocabulary (kept in lockstep with server/core/cards.py)
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1"

# Block types are the Epic 23 dataviz primitives (cards.py). The published-insight
# schema references them; it does not define a second vocabulary.
ALLOWED_BLOCK_TYPES = frozenset(
    {"kpi_row", "line", "bar", "gauge", "donut", "funnel", "table", "comment"}
)

# Card modes: template = A (ratified), compose = B (reserved, disabled by default).
CARD_MODE_TEMPLATE = "template"
CARD_MODE_COMPOSE = "compose"
ALLOWED_CARD_MODES = frozenset({CARD_MODE_TEMPLATE, CARD_MODE_COMPOSE})

CONFIDENCE_LEVELS = frozenset({"low", "medium", "high"})

# Non-additive metrics resolve through the semantic layer, never LLM arithmetic
# (mirrors cards._NON_ADDITIVE_METRICS). A published payload must not carry a
# free-form numeric value for these — the server resolves every number.
NON_ADDITIVE_METRICS = frozenset({"average_position", "roas", "ctr", "cpa"})

# ---------------------------------------------------------------------------
# Budgets (Epic 35 §8). No overflow truncates silently: publication is refused.
# ---------------------------------------------------------------------------

MAX_INSIGHTS_PER_DAY = 3  # slots 0..2 per (project_id, insight_date)
MAX_BLOCKS_PER_CARD = 8
MAX_POINTS_PER_SERIES = 30
MAX_TABLE_ROWS = 14
MAX_AGENT_SPEC_BYTES = 64 * 1024  # the spec the agent submits
MAX_PAYLOAD_BYTES = 512 * 1024  # the final server-enriched published payload


# ---------------------------------------------------------------------------
# JSON schema (draft-07 flavored dict; kept dependency-free / self-validated).
# ---------------------------------------------------------------------------

PUBLISHED_INSIGHT_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "toorow published daily insight",
    "type": "object",
    "additionalProperties": False,
    "required": ["schemaVersion", "slot", "insight", "period", "card"],
    "properties": {
        "schemaVersion": {"type": "string", "const": SCHEMA_VERSION},
        "slot": {"type": "integer", "minimum": 0, "maximum": MAX_INSIGHTS_PER_DAY - 1},
        "insight": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "summary", "whyItMatters", "confidence"],
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 200},
                "summary": {"type": "string", "minLength": 1, "maxLength": 1000},
                "whyItMatters": {"type": "string", "minLength": 1, "maxLength": 1000},
                "recommendedAction": {"type": "string", "maxLength": 1000},
                "confidence": {"type": "string", "enum": sorted(CONFIDENCE_LEVELS)},
                "limitations": {"type": "array", "items": {"type": "string"}},
            },
        },
        "period": {
            "type": "object",
            "additionalProperties": False,
            "required": ["dateFrom", "dateTo"],
            "properties": {
                "dateFrom": {"type": "string", "format": "date"},
                "dateTo": {"type": "string", "format": "date"},
                "comparison": {"type": "string"},
            },
        },
        "card": {
            "type": "object",
            "additionalProperties": False,
            "required": ["mode"],
            "properties": {
                "mode": {"type": "string", "enum": sorted(ALLOWED_CARD_MODES)},
                # A path: a template id resolved by cards.get_template(...)
                "template": {"type": "string"},
                "metrics": {"type": "array", "items": {"type": "string"}},
                "reportRef": {"type": "string"},
                "planId": {"type": "string"},
                # B path: declarative blocks (reserved, disabled by default)
                "blocks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["type", "binding"],
                        "properties": {
                            "type": {"type": "string", "enum": sorted(ALLOWED_BLOCK_TYPES)},
                            "title": {"type": "string"},
                            "binding": {"type": "object"},
                        },
                    },
                },
            },
        },
        "evidenceRefs": {"type": "array", "items": {"type": "string"}},
        "sourceRefs": {"type": "array", "items": {"type": "string"}},
    },
}


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of ``validate_published_insight``.

    ``ok`` True means every gate passed. Otherwise ``reason_code`` is the FIRST
    failing gate (stable identifier, safe to branch on) and ``message`` is a
    human-actionable explanation. ``field_path`` points at the offending location
    when meaningful.
    """

    ok: bool
    reason_code: str | None = None
    message: str | None = None
    field_path: str | None = None


def _fail(code: str, message: str, field_path: str | None = None) -> ValidationResult:
    return ValidationResult(ok=False, reason_code=code, message=message, field_path=field_path)


def canonical_payload_hash(payload: dict) -> str:
    """Deterministic SHA-256 of a payload (sorted keys, compact separators).

    The published artifact is self-contained and hashed so a share stays verifiable
    even if the originating run disappears (Epic 35 §7).
    """

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _shape_errors(payload: dict) -> ValidationResult | None:
    """Minimal, dependency-free structural check against PUBLISHED_INSIGHT_SCHEMA.

    Not a full JSON-Schema engine: it enforces the constraints the gate relies on
    (types, required keys, no unknown keys, enums, const version). Anything it lets
    through is caught by the semantic gates below.
    """

    if not isinstance(payload, dict):
        return _fail("schema_shape", "payload must be a JSON object", "$")

    if payload.get("schemaVersion") != SCHEMA_VERSION:
        return _fail(
            "schema_version",
            f"schemaVersion must be '{SCHEMA_VERSION}', got {payload.get('schemaVersion')!r}",
            "schemaVersion",
        )

    for key in ("slot", "insight", "period", "card"):
        if key not in payload:
            return _fail("schema_shape", f"missing required key '{key}'", key)

    allowed_top = set(PUBLISHED_INSIGHT_SCHEMA["properties"].keys())
    unknown = set(payload) - allowed_top
    if unknown:
        return _fail("schema_shape", f"unknown top-level keys: {sorted(unknown)}", "$")

    slot = payload["slot"]
    if (
        not isinstance(slot, int)
        or isinstance(slot, bool)
        or not (0 <= slot < MAX_INSIGHTS_PER_DAY)
    ):
        return _fail(
            "schema_shape", f"slot must be an integer in [0, {MAX_INSIGHTS_PER_DAY - 1}]", "slot"
        )

    insight = payload["insight"]
    if not isinstance(insight, dict):
        return _fail("schema_shape", "insight must be an object", "insight")
    for key in ("title", "summary", "whyItMatters", "confidence"):
        if not insight.get(key):
            return _fail(
                "schema_shape", f"insight.{key} is required and non-empty", f"insight.{key}"
            )
    if insight["confidence"] not in CONFIDENCE_LEVELS:
        return _fail(
            "schema_shape",
            f"insight.confidence must be one of {sorted(CONFIDENCE_LEVELS)}",
            "insight.confidence",
        )

    period = payload["period"]
    if not isinstance(period, dict) or "dateFrom" not in period or "dateTo" not in period:
        return _fail("schema_shape", "period must carry dateFrom and dateTo", "period")

    card = payload["card"]
    if not isinstance(card, dict) or "mode" not in card:
        return _fail("schema_shape", "card must carry a mode", "card")
    if card["mode"] not in ALLOWED_CARD_MODES:
        return _fail(
            "schema_shape", f"card.mode must be one of {sorted(ALLOWED_CARD_MODES)}", "card.mode"
        )

    return None


def validate_published_insight(
    payload: dict,
    *,
    available_metrics: set[str],
    available_dimensions: set[str],
    available_templates: set[str],
    resolvable_evidence: set[str],
    freshness_date: str | None,
    has_project_access: bool,
    existing_slots: set[int],
    expected_hash: str | None = None,
    final_payload_bytes: int | None = None,
    allow_compose: bool = False,
    allowed_block_types: frozenset[str] = ALLOWED_BLOCK_TYPES,
) -> ValidationResult:
    """Fail-closed validation of one published-insight payload.

    Enforces the Epic 35 §8 order and returns the FIRST failing gate. Pure: all
    server-resolved facts are inputs.

    Gate order (reason_code):
      1. schema_shape / schema_version / *_too_large / budget_*  (structure + budgets)
      2. project_scope / idempotency                              (access + slot)
      3. mode_not_enabled / primitive_not_allowed / binding_not_allowed
      4. template_unknown / metric_unavailable / dimension_unavailable
      5. evidence_unresolved
      6. stale_data / period_invalid
      7. ratio_semantic
      8. hash_mismatch
    """

    # --- Gate 1a: structure + version -------------------------------------
    shape_error = _shape_errors(payload)
    if shape_error is not None:
        return shape_error

    # --- Gate 1b: size budgets (agent spec) -------------------------------
    spec_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if spec_bytes > MAX_AGENT_SPEC_BYTES:
        return _fail(
            "spec_too_large",
            f"agent spec {spec_bytes}B exceeds {MAX_AGENT_SPEC_BYTES}B budget",
            "$",
        )
    if final_payload_bytes is not None and final_payload_bytes > MAX_PAYLOAD_BYTES:
        return _fail(
            "payload_too_large",
            f"published payload {final_payload_bytes}B exceeds {MAX_PAYLOAD_BYTES}B budget",
            "$",
        )

    card = payload["card"]
    blocks = card.get("blocks") or []
    if not isinstance(blocks, list):
        return _fail("schema_shape", "card.blocks must be an array", "card.blocks")
    if len(blocks) > MAX_BLOCKS_PER_CARD:
        return _fail(
            "budget_blocks",
            f"{len(blocks)} blocks exceeds {MAX_BLOCKS_PER_CARD}/card budget",
            "card.blocks",
        )

    # --- Gate 2: project access + idempotency -----------------------------
    if not has_project_access:
        return _fail("project_scope", "identity has no access to the target project", "$")
    slot = payload["slot"]
    if slot in existing_slots:
        return _fail("idempotency", f"slot {slot} already published for this project/date", "slot")

    # --- Gate 3: mode + primitive/binding allowlist -----------------------
    mode = card["mode"]
    if mode == CARD_MODE_COMPOSE and not allow_compose:
        return _fail(
            "mode_not_enabled",
            "card.mode 'compose' (option B) is not enabled; use 'template' (option A)",
            "card.mode",
        )
    for idx, block in enumerate(blocks):
        if not isinstance(block, dict):
            return _fail(
                "schema_shape", f"card.blocks[{idx}] must be an object", f"card.blocks[{idx}]"
            )
        btype = block.get("type")
        if btype not in allowed_block_types:
            return _fail(
                "primitive_not_allowed",
                f"block type {btype!r} is not an allowlisted primitive",
                f"card.blocks[{idx}].type",
            )
        binding = block.get("binding")
        if not isinstance(binding, dict):
            return _fail(
                "binding_not_allowed",
                f"card.blocks[{idx}].binding must be an object",
                f"card.blocks[{idx}].binding",
            )
        # Per-block series/table budgets (no silent truncation).
        limit = binding.get("limit")
        if btype == "table" and isinstance(limit, int) and limit > MAX_TABLE_ROWS:
            return _fail(
                "budget_table_rows",
                f"table limit {limit} exceeds {MAX_TABLE_ROWS}-row budget",
                f"card.blocks[{idx}].binding.limit",
            )
        points = binding.get("points")
        if btype in ("line", "bar") and isinstance(points, int) and points > MAX_POINTS_PER_SERIES:
            return _fail(
                "budget_points",
                f"series points {points} exceeds {MAX_POINTS_PER_SERIES} budget",
                f"card.blocks[{idx}].binding.points",
            )

    # --- Gate 4: template / metric / dimension availability ---------------
    if mode == CARD_MODE_TEMPLATE:
        template = card.get("template")
        if not template:
            return _fail(
                "template_unknown", "card.mode 'template' requires a template id", "card.template"
            )
        if template not in available_templates:
            return _fail(
                "template_unknown",
                f"template {template!r} is not in the project's available templates",
                "card.template",
            )

    referenced_metrics = _collect_metrics(card)
    for metric in referenced_metrics:
        if metric != "*" and metric not in available_metrics:
            return _fail(
                "metric_unavailable",
                f"metric {metric!r} is not available for this project",
                "card",
            )
    referenced_dimensions = _collect_dimensions(card)
    for dim in referenced_dimensions:
        if dim not in available_dimensions:
            return _fail(
                "dimension_unavailable",
                f"dimension {dim!r} is not available for this project",
                "card",
            )

    # --- Gate 5: evidence refs resolve inside server data -----------------
    for ref in payload.get("evidenceRefs") or []:
        if ref not in resolvable_evidence:
            return _fail(
                "evidence_unresolved",
                f"evidence ref {ref!r} does not resolve in server data",
                "evidenceRefs",
            )

    # --- Gate 6: freshness / period vs claimed date -----------------------
    period = payload["period"]
    d_from = _iso_date(period["dateFrom"])
    d_to = _iso_date(period["dateTo"])
    if d_from is None or d_to is None:
        return _fail("period_invalid", "period.dateFrom/dateTo must be ISO dates", "period")
    if d_from > d_to:
        return _fail("period_invalid", "period.dateFrom is after period.dateTo", "period")
    fresh = _iso_date(freshness_date) if freshness_date else None
    if fresh is None:
        return _fail(
            "stale_data", "no resolved freshness date; cannot present data as current", "$"
        )
    if d_to > fresh:
        return _fail(
            "stale_data",
            f"period.dateTo {d_to.isoformat()} is beyond resolved freshness {fresh.isoformat()}",
            "period.dateTo",
        )

    # --- Gate 7: non-additive metrics must not carry free numbers ---------
    #   The model selects and explains; the server resolves every number. A payload
    #   may reference a non-additive metric by name, but must never inline its value.
    ratio_error = _reject_free_ratios(card)
    if ratio_error is not None:
        return ratio_error

    # --- Gate 8: payload hash --------------------------------------------
    if expected_hash is not None:
        actual = canonical_payload_hash(payload)
        if actual != expected_hash:
            return _fail("hash_mismatch", "payload hash does not match expected hash", "$")

    return ValidationResult(ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_metrics(card: dict) -> set[str]:
    metrics: set[str] = set()
    for m in card.get("metrics") or []:
        if isinstance(m, str):
            metrics.add(m)
    for block in card.get("blocks") or []:
        binding = block.get("binding") if isinstance(block, dict) else None
        if not isinstance(binding, dict):
            continue
        val = binding.get("metrics")
        if isinstance(val, str):
            metrics.add(val)
        elif isinstance(val, list):
            metrics.update(x for x in val if isinstance(x, str))
        for key in ("numerator", "denominator", "metric"):
            v = binding.get(key)
            if isinstance(v, str):
                metrics.add(v)
    return metrics


def _collect_dimensions(card: dict) -> set[str]:
    dims: set[str] = set()
    for block in card.get("blocks") or []:
        binding = block.get("binding") if isinstance(block, dict) else None
        if not isinstance(binding, dict):
            continue
        val = binding.get("dimensions")
        if isinstance(val, str):
            dims.add(val)
        elif isinstance(val, list):
            dims.update(x for x in val if isinstance(x, str))
    return dims


def _reject_free_ratios(card: dict) -> ValidationResult | None:
    """Reject any inline numeric value attached to a non-additive metric.

    The published payload carries an editorial claim and a card *contract*; it never
    carries resolved figures for ratio/weighted metrics — those come from the semantic
    layer at render time (Epic 35 §8 step 7).
    """

    for idx, block in enumerate(card.get("blocks") or []):
        binding = block.get("binding") if isinstance(block, dict) else None
        if not isinstance(binding, dict):
            continue
        metric = binding.get("metrics")
        names = {metric} if isinstance(metric, str) else set(metric or [])
        if names & NON_ADDITIVE_METRICS and "value" in binding:
            return _fail(
                "ratio_semantic",
                f"non-additive metric in card.blocks[{idx}] must not inline a 'value'; "
                "the semantic layer resolves it",
                f"card.blocks[{idx}].binding.value",
            )
    return None
