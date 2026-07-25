"""toorow -- Semi-auto plan<->actual mapping suggestions (Story 27.5, Epic 27).

The last missing link of the forecast/actual thread. Epic 22 shipped the MANUAL N:M
``plan_line_mappings`` (a human declares each plan-line <-> (connector, campaign)
rapprochement, split weights Sum=1.0). This module adds the SEMI-AUTO half: propose,
by name/taxonomy similarity, which real campaign matches which plan line -- the human
then only has to VALIDATE (never an autonomous AI apply; the invariant shared with 27.4
and Jean's synthese Sec.6/Sec.7).

STRICTLY PASSIVE / READ-ONLY. This module NEVER mutates anything:
  * it reuses the PURE similarity pipeline of 27.4 (normalize_value / similarity,
    IMPORTED, never copied -- single source of truth) to pair plan-line labels with
    real campaign refs;
  * it reuses compute_default_splits of 22.3 for the optional indicative weight;
  * it only BUILDS the ``entries`` payload that a human/API passes to
    ``mediaplan_mapping.set_line_mappings`` (the sole write path of epic 22, with its
    transaction, anti-course lock, Sum=1.0 validation and audit). It NEVER calls
    set_line_mappings itself and emits no INSERT/UPDATE/DELETE.

Design mirrors dimension_conformance.py / mediaplan_mapping.py: ``from __future__ import
annotations``, module logger, lazy core.* imports inside the I/O functions, pure
functions kept apart from I/O so the invariants are testable without Postgres.

AD-2 (ZERO provider vocabulary): this module contains NO connector name and NO dimension
name. The plan side is never a provider; real connector labels come from the caller / DB
rows, never from code. The chosen implementation (choice (b) below) iterates plan x real
directly, so no plan-side pseudo-connector sentinel is needed.

Windows/CI note: all log/message strings use ASCII-safe characters only.

Implementation choice (Completion Notes): the story offers two ways to reuse the 27.4
engine -- (a) feed suggest_mappings a dict {pseudo_plan -> labels, connector -> refs} and
filter its cross-connector output, or (b) call normalize_value + similarity DIRECTLY in a
plan x real loop. We pick (b): it reuses the SAME pure 27.4 functions without copying
them, and it reconstructs the (plan_side, real_side) EDGE we need directly and
deterministically -- suggest_mappings emits per-item Suggestions sharing a canonical
pivot, which would force a fragile edge reconstruction and a meaningless campaign 'pivot'
(the story: "the pivot has no business meaning here, we only keep the edge"). Choice
(b) is the more lisible + provably deterministic option (explicit sorts, tested).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.dimension_conformance import (  # PURE 27.4 engine -- imported, NOT copied
    DEFAULT_SIMILARITY_THRESHOLD,
    METHOD_EXACT,
    METHOD_NORMALIZED,
    METHOD_SIMILARITY,
    NormalizeOptions,
    normalize_value,
    similarity,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SIMILARITY_THRESHOLD",
    "LineMappingSuggestion",
    "propose_set_line_mappings_payload",
    "suggest_line_mappings",
    "suggest_line_mappings_for_plan",
]


# ---------------------------------------------------------------------------
# A.2 Plan lines (READ-ONLY via mediaplan_store).
# ---------------------------------------------------------------------------
# The active-version lines are read inline in suggest_line_mappings_for_plan (which also
# needs the plan's project_id from the SAME get_plan call), so no separate _plan_lines
# helper exists -- a single READ-ONLY get_plan is the sole plan read.


def _label_for(line: dict) -> str:
    """The string matched for a plan line: label, falling back to line_key if empty."""
    label = (line.get("label") or "").strip()
    if label:
        return label
    return (line.get("line_key") or "").strip()


# ---------------------------------------------------------------------------
# A.4 Suggestion -- PURE at the core.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LineMappingSuggestion:
    """One proposed plan-line <-> real-campaign pairing (PURE data, no persistence)."""

    line_key: str
    label: str  # the matched label (for human review)
    connector: str
    campaign_ref: str
    method: str  # 'exact' | 'normalized' | 'similarity' (27.4 vocabulary)
    score: float  # 1.0 for exact/normalized; difflib ratio for similarity
    normalized_form: str  # normalized form of the plan label (shared identity key)


def _classify(
    plan_norm: str, actual_norm: str, threshold: float
) -> tuple[str, float] | None:
    """Classify a (plan_label, campaign_ref) pair by its normalized forms (PURE).

    Mirrors the 27.4 stage order, safest to fuzziest, one stage per pair:
      * raw-equal is decided by the caller (it compares raw strings before normalizing);
      * normalized forms identical -> 'normalized', score 1.0;
      * difflib ratio >= threshold -> 'similarity', score = ratio;
      * below the threshold -> None (honest: no suggestion).
    An empty normalized form never matches (not a matchable identity, mirrors 27.4).
    """
    if not plan_norm or not actual_norm:
        return None
    if plan_norm == actual_norm:
        return (METHOD_NORMALIZED, 1.0)
    score = similarity(plan_norm, actual_norm)
    if score >= threshold:
        return (METHOD_SIMILARITY, score)
    return None


def suggest_line_mappings(
    lines: list[dict],
    actuals_by_connector: dict[str, list[str]],
    *,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    normalize_options: NormalizeOptions = NormalizeOptions(),
) -> list[LineMappingSuggestion]:
    """PURE. Pair each plan-line label with the similar real campaign(s), via 27.4.

    Mechanics (documented): for every plan line and every real (connector,
    campaign_ref) we compare -- reusing the SAME pure 27.4 functions -- the RAW forms
    (exact) then the NORMALIZED forms (normalize_value: case / separators / bracket tags
    / datestamps / diacritics) then the difflib ``similarity`` of the normalized forms.
    Because we iterate plan x actual, a plan label is NEVER paired with another plan label
    (the cross-connector rule of 27.4 is structurally guaranteed here: the two sides are
    disjoint sets). No canonical pivot is created -- only the (plan, real) EDGE and its
    (method, score) are kept.

    N:M: a line_key can receive several suggestions (one per matching campaign), a
    campaign can match several lines -- both are emitted (no greedy consumption; the
    human validates the residual, cf. 27.5's "human validation" invariant).

    Below the threshold: NO suggestion (honest -- the human maps the rest manually).

    Deterministic: the output is sorted by (line_key, connector, campaign_ref, method).
    """
    # Pre-normalize the plan side once (label -> normalized), keep the line_key link.
    plan_entries: list[tuple[str, str, str]] = []  # (line_key, label, normalized)
    for line in lines:
        line_key = (line.get("line_key") or "").strip()
        if not line_key:
            continue
        label = _label_for(line)
        plan_entries.append((line_key, label, normalize_value(label, normalize_options)))

    suggestions: list[LineMappingSuggestion] = []
    # Deterministic iteration: connectors sorted, campaign refs de-duplicated + sorted.
    for connector in sorted(actuals_by_connector):
        seen_refs: set[str] = set()
        for campaign_ref in actuals_by_connector[connector]:
            if campaign_ref in seen_refs:
                continue
            seen_refs.add(campaign_ref)
            actual_norm = normalize_value(campaign_ref, normalize_options)
            for line_key, label, plan_norm in plan_entries:
                # Stage 1 -- EXACT on the RAW strings (safest, score 1.0).
                if label == campaign_ref:
                    method, score = METHOD_EXACT, 1.0
                else:
                    classified = _classify(
                        plan_norm, actual_norm, similarity_threshold
                    )
                    if classified is None:
                        continue
                    method, score = classified
                suggestions.append(
                    LineMappingSuggestion(
                        line_key=line_key,
                        label=label,
                        connector=connector,
                        campaign_ref=campaign_ref,
                        method=method,
                        score=score,
                        normalized_form=plan_norm,
                    )
                )

    suggestions.sort(
        key=lambda s: (s.line_key, s.connector, s.campaign_ref, s.method)
    )
    return suggestions


# ---------------------------------------------------------------------------
# A.5 Default-split proposal (reuses 22.3) -- builds the set_line_mappings payload.
# ---------------------------------------------------------------------------


def propose_set_line_mappings_payload(
    suggestions: list[LineMappingSuggestion],
    *,
    include_default_weights: bool = False,
) -> dict[str, list[dict]]:
    """Group suggestions by line_key into the ``entries`` payload of set_line_mappings.

    Output shape: ``{line_key -> [{connector, campaign_ref}, ...]}`` -- EXACTLY the
    ``entries`` list mediaplan_mapping.set_line_mappings consumes per line_key.

    split_weight is OMITTED by default: we invent no weight. set_line_mappings applies
    the implicit regime -- equirepartition 1/N PER CAMPAIGN (compute_default_splits,
    Sum=1.0) at write time. The payload is a PROPOSAL: a human passes it to
    set_line_mappings (validation), or edits it first. NO write happens here.

    include_default_weights=True annotates each entry with an INDICATIVE
    ``proposed_split_weight`` (Decimal string), computed by compute_default_splits over
    the line_keys that SHARE that (connector, campaign_ref) -- so the display can show the
    default that set_line_mappings would apply. This field is distinct from split_weight
    and is NEVER persisted as-is (it is display-only; the entries still omit split_weight).

    Deterministic: line_keys and their entries are emitted in sorted order.
    """
    # Group by line_key, de-duplicating (connector, campaign_ref) within a line.
    by_line: dict[str, list[tuple[str, str]]] = {}
    for sug in suggestions:
        pairs = by_line.setdefault(sug.line_key, [])
        key = (sug.connector, sug.campaign_ref)
        if key not in pairs:
            pairs.append(key)

    proposed_weights: dict[tuple[str, str], dict[str, str]] = {}
    if include_default_weights:
        from core.mediaplan_mapping import compute_default_splits  # noqa: PLC0415

        # For each (connector, campaign_ref), the set of line_keys that map it.
        lines_per_campaign: dict[tuple[str, str], list[str]] = {}
        for line_key, pairs in by_line.items():
            for pair in pairs:
                lines_per_campaign.setdefault(pair, []).append(line_key)
        for pair, line_keys in lines_per_campaign.items():
            splits = compute_default_splits(line_keys)
            proposed_weights[pair] = {k: format(v, "f") for k, v in splits.items()}

    payload: dict[str, list[dict]] = {}
    for line_key in sorted(by_line):
        entries: list[dict] = []
        for connector, campaign_ref in sorted(by_line[line_key]):
            entry: dict = {"connector": connector, "campaign_ref": campaign_ref}
            if include_default_weights:
                entry["proposed_split_weight"] = proposed_weights[
                    (connector, campaign_ref)
                ][line_key]
            entries.append(entry)
        payload[line_key] = entries
    return payload


# ---------------------------------------------------------------------------
# A.6 Read orchestration (fine I/O, testable by injection).
# ---------------------------------------------------------------------------


def _plan_window(lines: list[dict]) -> tuple[str | None, str | None]:
    """Envelope [min(start_date), max(end_date)] of the active lines (ISO strings).

    list_lines renders start_date/end_date as ISO 'YYYY-MM-DD' strings, so a plain
    min/max over the non-empty strings is the calendar envelope. Empty lines -> (None,
    None). Mirrors list_unmapped_actuals' window computation.
    """
    starts = [s for s in (line.get("start_date") for line in lines) if s]
    ends = [e for e in (line.get("end_date") for line in lines) if e]
    if not starts or not ends:
        return (None, None)
    return (min(starts), max(ends))


def _actuals_from_warehouse(
    campaign_spend_fn, project_id: str, start: str, end: str
) -> dict[str, list[str]]:
    """Read the real campaigns of the window via the injected warehouse helper.

    query_campaign_spend returns [{connector, campaign_ref, spend}, ...]. We keep only
    the (connector, campaign_ref) identities to pair. WarehouseUnavailable PROPAGATES
    (never a silent [] pretending 'no campaign to map' -- AD-9).
    """
    rows = campaign_spend_fn(project_id, start, end)
    out: dict[str, list[str]] = {}
    for row in rows:
        connector = row.get("connector")
        campaign_ref = row.get("campaign_ref")
        if connector is None or campaign_ref is None:
            continue
        out.setdefault(connector, [])
        if campaign_ref not in out[connector]:
            out[connector].append(campaign_ref)
    return out


def suggest_line_mappings_for_plan(
    plan_id: str,
    *,
    actuals_by_connector: dict[str, list[str]] | None = None,
    campaign_spend_fn=None,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    normalize_options: NormalizeOptions = NormalizeOptions(),
) -> dict:
    """End-to-end READ: suggest plan<->actual mappings for a plan (no write).

    Reads the active-version lines (mediaplan_store), obtains the real campaigns from the
    CALLER (``actuals_by_connector``, which always wins) OR, when None, from the injected
    warehouse helper over the plan window (``campaign_spend_fn``, default
    warehouse.query_campaign_spend). Returns:

        {plan_id, window: {start, end} | None,
         suggestions: [LineMappingSuggestion-as-dict, ...],
         payload_by_line_key: {line_key -> [{connector, campaign_ref}, ...]},
         notes: [...]}

    NO write. Unknown plan / inactive version -> empty suggestions + an honest note.
    WarehouseUnavailable PROPAGATES (never a fake empty perimeter -- AD-9).
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_store import get_plan  # noqa: PLC0415

    notes: list[str] = []
    with get_connection() as conn:
        plan = get_plan(conn, plan_id=plan_id)
    lines = list(plan.get("lines") or []) if plan is not None else []
    project_id = plan.get("project_id") if plan is not None else None

    if not lines:
        # Unknown plan OR inactive version OR versionless plan -> honest, no crash.
        notes.append("no active plan lines (unknown plan or inactive version)")

    window_start, window_end = _plan_window(lines)
    window = (
        {"start": window_start, "end": window_end}
        if window_start is not None and window_end is not None
        else None
    )

    if actuals_by_connector is None:
        if lines and window is not None:
            fn = campaign_spend_fn
            if fn is None:
                from core import warehouse as _wh  # noqa: PLC0415

                fn = _wh.query_campaign_spend
            # WarehouseUnavailable propagates out of this call (AD-9).
            actuals_by_connector = _actuals_from_warehouse(
                fn, project_id, window_start, window_end
            )
        else:
            actuals_by_connector = {}
            notes.append("no window to read real campaigns from")

    suggestions = suggest_line_mappings(
        lines,
        actuals_by_connector,
        similarity_threshold=similarity_threshold,
        normalize_options=normalize_options,
    )
    payload_by_line_key = propose_set_line_mappings_payload(suggestions)

    return {
        "plan_id": str(plan_id),
        "window": window,
        "suggestions": [
            {
                "line_key": s.line_key,
                "label": s.label,
                "connector": s.connector,
                "campaign_ref": s.campaign_ref,
                "method": s.method,
                "score": s.score,
                "normalized_form": s.normalized_form,
            }
            for s in suggestions
        ],
        "payload_by_line_key": payload_by_line_key,
        "notes": notes,
    }
