"""toorow -- Confidence-scored tolerant column recognition (Story 22.17, FR3).

Non-positional recognition: match a file's source columns to the template's
canonical fields BY MEANING (renamed / reordered / other-language headers), each
with a confidence score, ignoring extra columns. Row-level noise (empty rows,
subtotal/total rows) is skipped by the producer (Story 22.10); this module is the
COLUMN recognizer that PROPOSES the {source -> canonical} mapping the gate
(22.15) and producer (22.12) then consume.

Name matching is alias/synonym-driven: the template contract declares, per
canonical field id, a list of alternative names (including other languages) under
``aliases``; a fallback name is derived from the id. Confidence REUSES the Story
12.3 ``datastream_field_mapping`` scoring (physical type + sample evidence) so the
preview speaks one confidence/ambiguity vocabulary -- no second scorer.

Pure module (no I/O): offline-testable on a header list + a bounded sample.
"""

from __future__ import annotations

import difflib
import unicodedata
from typing import Any

from core.datastream_field_mapping import profile_fields

# A source column whose best canonical match scores below this is an EXTRA column
# (ignored, never force-mapped). Between the best and the runner-up, a gap smaller
# than the margin (both above threshold) is an AMBIGUOUS match (flagged).
DEFAULT_MATCH_THRESHOLD = 0.6
DEFAULT_AMBIGUITY_MARGIN = 0.12


def normalize_name(value: str) -> str:
    """Fold a header/alias to a comparable token string.

    Lowercase, strip accents to ASCII, drop non-alphanumerics to single spaces.
    So "Bruttokosten Gesamt", "brutto-kosten gesamt", and "BRUTTOKOSTEN  GESAMT"
    all normalize to "bruttokosten gesamt".
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    out: list[str] = []
    prev_space = False
    for ch in text:
        if ch.isalnum():
            out.append(ch)
            prev_space = False
        elif not prev_space:
            out.append(" ")
            prev_space = True
    return "".join(out).strip()


def _humanize_id(canonical_id: str) -> str:
    """Derive a fallback matchable name from a canonical id (mdm_net_cost -> net cost)."""
    base = canonical_id
    for prefix in ("mdm_", "dim_", "metric_"):
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    return normalize_name(base.replace("_", " "))


def _name_similarity(source: str, alias: str) -> float:
    """Similarity in [0,1] between two already-normalized names.

    Exact match -> 1.0; otherwise the max of a token-set Jaccard and a difflib
    sequence ratio, with a boost when one name's tokens are a subset of the other
    (a renamed column that adds/keeps words, e.g. "cost" vs "net cost").
    """
    if not source or not alias:
        return 0.0
    if source == alias:
        return 1.0
    s_tokens, a_tokens = set(source.split()), set(alias.split())
    inter = s_tokens & a_tokens
    union = s_tokens | a_tokens
    jaccard = len(inter) / len(union) if union else 0.0
    subset_boost = 0.85 if (s_tokens <= a_tokens or a_tokens <= s_tokens) and inter else 0.0
    ratio = difflib.SequenceMatcher(None, source, alias).ratio()
    return max(jaccard, subset_boost, ratio)


def _candidate_names(contract: dict[str, Any]) -> dict[str, list[str]]:
    """Build {canonical_id -> [normalized matchable names]} from the template.

    Names = the declared aliases (incl. other languages) + a fallback derived from
    the id. Candidates are the template's required + optional canonical fields.
    """
    required = list(contract.get("required_fields") or [])
    optional = list(contract.get("optional_fields") or [])
    aliases = contract.get("aliases") or {}
    names: dict[str, list[str]] = {}
    for cid in required + optional:
        variants = {_humanize_id(cid)}
        for alt in aliases.get(cid, []) or []:
            variants.add(normalize_name(alt))
        names[cid] = sorted(v for v in variants if v)
    return names


def _best_matches(
    source_norm: str, candidates: dict[str, list[str]]
) -> list[tuple[str, float]]:
    """Return (canonical_id, score) sorted best-first for one source column."""
    scored = [
        (cid, max((_name_similarity(source_norm, name) for name in names), default=0.0))
        for cid, names in candidates.items()
    ]
    scored.sort(key=lambda kv: (-kv[1], kv[0]))
    return scored


def recognize_columns(
    template: dict[str, Any],
    source_columns: list[str],
    sample_rows: list[dict[str, Any]] | None = None,
    *,
    match_threshold: float = DEFAULT_MATCH_THRESHOLD,
    ambiguity_margin: float = DEFAULT_AMBIGUITY_MARGIN,
) -> dict[str, Any]:
    """Recognize source columns against the template's canonical fields (FR3).

    Returns::

      {
        "fields": [ {source_column, canonical_target, status, name_confidence,
                     confidence, evidence}, ... ],   # one per source column
        "mapping": {source_column: canonical_id, ...},  # confident matches only
        "ambiguities": [ ... ],                          # name + profile ambiguities
      }

    status ∈ {matched, ambiguous, unmatched}. An unmatched column is an extra
    column, ignored (never force-mapped). ``confidence`` fuses the name-match
    score with the reused ``profile_fields`` physical/sample confidence.
    """
    contract = template.get("contract") if "contract" in template else template
    candidates = _candidate_names(contract or {})

    fields: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    ambiguities: list[dict[str, Any]] = []

    for source_col in source_columns:
        source_norm = normalize_name(source_col)
        ranked = _best_matches(source_norm, candidates)
        best_id, best_score = (ranked[0] if ranked else (None, 0.0))
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0

        if best_id is None or best_score < match_threshold:
            fields.append({
                "source_column": source_col, "canonical_target": None,
                "status": "unmatched", "name_confidence": round(best_score, 4),
                "confidence": round(best_score, 4),
                "evidence": ["no canonical name above threshold"],
            })
            continue

        status = "matched"
        if best_score - second_score < ambiguity_margin and second_score >= match_threshold:
            status = "ambiguous"
            ambiguities.append({
                "code": "ambiguous_column_match",
                "source_column": source_col,
                "candidates": [ranked[0][0], ranked[1][0]],
            })
        else:
            mapping[source_col] = best_id

        fields.append({
            "source_column": source_col, "canonical_target": best_id,
            "status": status, "name_confidence": round(best_score, 4),
            "confidence": round(best_score, 4),  # refined below with profile evidence
            "evidence": [f"name~{best_id}:{round(best_score, 2)}"],
        })

    # Collision: two source columns matched (confidently) to the SAME canonical.
    seen: dict[str, str] = {}
    for source_col, cid in mapping.items():
        if cid in seen:
            ambiguities.append({
                "code": "duplicate_target",
                "canonical_target": cid,
                "source_columns": sorted([seen[cid], source_col]),
            })
        else:
            seen[cid] = source_col

    # Reuse the 12.3 confidence + ambiguity engine over the recognized mapping.
    if mapping:
        _fuse_profile_confidence(fields, mapping, sample_rows or [], ambiguities)

    return {"fields": fields, "mapping": mapping, "ambiguities": ambiguities}


def _fuse_profile_confidence(
    fields: list[dict[str, Any]],
    mapping: dict[str, str],
    sample_rows: list[dict[str, Any]],
    ambiguities: list[dict[str, Any]],
) -> None:
    """Fuse each matched field's name confidence with the reused profile confidence."""
    from core.file_source_gate import _infer_physical_type  # noqa: PLC0415

    field_records = []
    for source_col, cid in mapping.items():
        values = [row.get(source_col) for row in sample_rows]
        physical_type = _infer_physical_type(values)
        kind = "metric" if physical_type == "decimal" else (
            "date" if physical_type == "date" else "dimension"
        )
        field_records.append({
            "field_id": source_col, "canonical_target": cid,
            "physical_type": physical_type, "kind": kind,
        })
    profile = profile_fields(field_records=field_records, sample_data=sample_rows)
    prof_by_source = {
        f["field_id"]: (f.get("profile") or {}).get("confidence", 0.0)
        for f in profile["fields"]
    }
    for f in fields:
        src = f["source_column"]
        if src in prof_by_source:
            name_c = f["name_confidence"]
            prof_c = prof_by_source[src]
            f["profile_confidence"] = round(prof_c, 4)
            f["confidence"] = round(0.5 * name_c + 0.5 * prof_c, 4)
    for amb in profile.get("ambiguities", []):
        ambiguities.append(amb)
