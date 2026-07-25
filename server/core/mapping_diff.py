"""toorow -- Human-readable mapping-version diff generator (Story 36.17, Epic 36).

``diff_mappings(before_version_row, after_version_row) -> dict`` produces a
DETERMINISTIC, human-readable diff between two immutable mapping versions for the
governed review screen (Story 36.18) and the proposal record.

INVARIANTS
----------
* NO raw provider values. The diff is computed from field NAMES, physical TYPES,
  canonical/mdm TARGETS, semantic roles, currency and additivity flags, plus the
  content/source-schema HASHES -- never a provider sample row or observed value.
  (E36-NFR01: provider schema/values/samples are untrusted.)
* Deterministic: the same (before, after) pair always yields an equal dict --
  every collection is sorted, so two calls are byte-identical when re-serialized.
* Source-agnostic (AD-2): field names arrive from the mapping payload rows, never
  from any hardcoded provider vocabulary.

The diff surfaces the changes a reviewer must reason about before publication:
field additions/removals, physical-type changes, canonical-target rebindings, and
currency / non-additive (measure) conflicts introduced or resolved.
"""

from __future__ import annotations

from typing import Any


def _fields_by_id(version_row: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Index a version row's fields by ``field_id`` (empty dict for a null side).

    Reads the ``mapping_payload.fields`` list. A missing/invalid payload yields an
    empty index so an added-from-nothing or removed-to-nothing diff is well-defined.
    """
    if not isinstance(version_row, dict):
        return {}
    payload = version_row.get("mapping_payload")
    if not isinstance(payload, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for field in payload.get("fields") or []:
        if not isinstance(field, dict):
            continue
        fid = field.get("field_id")
        if isinstance(fid, str) and fid:
            out[fid] = field
    return out


def _safe_type(field: dict[str, Any]) -> str:
    """Return the field's physical type as a bounded token (never a provider blob)."""
    pt = field.get("physical_type")
    if isinstance(pt, str) and pt and len(pt) <= 64:
        return pt
    return "unknown"


def _safe_role(field: dict[str, Any]) -> str:
    role = (field.get("suggestion") or {}).get("semantic_role")
    if isinstance(role, str) and role and len(role) <= 64:
        return role
    return "dimension"


def _safe_target(field: dict[str, Any]) -> str | None:
    """Return the bound canonical/mdm target handle, or None. No provider values."""
    binding = field.get("binding") or {}
    mdm = binding.get("mdm_target")
    if isinstance(mdm, str) and mdm:
        return mdm
    canonical = binding.get("canonical_target")
    if isinstance(canonical, str) and canonical and len(canonical) <= 128:
        return canonical
    return None


def _safe_currency(field: dict[str, Any]) -> str:
    cur = (field.get("suggestion") or {}).get("currency")
    if isinstance(cur, str) and cur and len(cur) <= 16:
        return cur
    return "unknown"


def _non_additive(field: dict[str, Any]) -> bool:
    return bool((field.get("suggestion") or {}).get("non_additive", False))


def _version_meta(version_row: dict[str, Any] | None) -> dict[str, Any]:
    """Return the bounded, safe version metadata for the diff header."""
    if not isinstance(version_row, dict):
        return {"version_id": None, "version_number": None, "content_hash": None,
                "source_schema_hash": None}
    return {
        "version_id": version_row.get("id"),
        "version_number": version_row.get("version_number"),
        "content_hash": version_row.get("content_hash"),
        "source_schema_hash": version_row.get("source_schema_hash"),
    }


def diff_mappings(
    before_version_row: dict[str, Any] | None,
    after_version_row: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a human-readable, deterministic diff of two mapping versions.

    ``before_version_row`` may be None (the Datastream had no live mapping yet).
    The result shape::

        {
          "before": {version_id, version_number, content_hash, source_schema_hash},
          "after":  {...},
          "unchanged": <bool>,          # content hashes equal
          "schema_changed": <bool>,     # source_schema_hash differs
          "added_fields":   [field_id, ...],       # in after, not before
          "removed_fields": [field_id, ...],       # in before, not after
          "type_changes":   [{field_id, from, to}, ...],
          "target_rebindings": [{field_id, from, to}, ...],
          "role_changes":   [{field_id, from, to}, ...],
          "currency_conflicts":   [{field_id, from, to}, ...],
          "non_additive_changes": [{field_id, from, to}, ...],
          "summary": "<one-line French human summary>",
        }

    Every list is sorted so the output is deterministic. NO raw provider value ever
    appears -- only field names, physical types, target handles, roles, currency
    codes, additivity flags and hashes. (E36-NFR01.)
    """
    before = _fields_by_id(before_version_row)
    after = _fields_by_id(after_version_row)

    before_ids = set(before)
    after_ids = set(after)

    added = sorted(after_ids - before_ids)
    removed = sorted(before_ids - after_ids)

    type_changes: list[dict[str, Any]] = []
    target_rebindings: list[dict[str, Any]] = []
    role_changes: list[dict[str, Any]] = []
    currency_conflicts: list[dict[str, Any]] = []
    non_additive_changes: list[dict[str, Any]] = []

    for fid in sorted(before_ids & after_ids):
        b = before[fid]
        a = after[fid]

        b_type, a_type = _safe_type(b), _safe_type(a)
        if b_type != a_type:
            type_changes.append({"field_id": fid, "from": b_type, "to": a_type})

        b_target, a_target = _safe_target(b), _safe_target(a)
        if b_target != a_target:
            target_rebindings.append({"field_id": fid, "from": b_target, "to": a_target})

        b_role, a_role = _safe_role(b), _safe_role(a)
        if b_role != a_role:
            role_changes.append({"field_id": fid, "from": b_role, "to": a_role})

        b_cur, a_cur = _safe_currency(b), _safe_currency(a)
        if b_cur != a_cur:
            currency_conflicts.append({"field_id": fid, "from": b_cur, "to": a_cur})

        b_na, a_na = _non_additive(b), _non_additive(a)
        if b_na != a_na:
            non_additive_changes.append({"field_id": fid, "from": b_na, "to": a_na})

    before_meta = _version_meta(before_version_row)
    after_meta = _version_meta(after_version_row)
    unchanged = (
        before_meta["content_hash"] is not None
        and before_meta["content_hash"] == after_meta["content_hash"]
    )
    schema_changed = before_meta["source_schema_hash"] != after_meta["source_schema_hash"]

    parts: list[str] = []
    if added:
        parts.append(f"{len(added)} champ(s) ajoute(s)")
    if removed:
        parts.append(f"{len(removed)} champ(s) retire(s)")
    if type_changes:
        parts.append(f"{len(type_changes)} changement(s) de type")
    if target_rebindings:
        parts.append(f"{len(target_rebindings)} re-liaison(s) de cible canonique")
    if role_changes:
        parts.append(f"{len(role_changes)} changement(s) de role")
    if currency_conflicts:
        parts.append(f"{len(currency_conflicts)} conflit(s) de devise")
    if non_additive_changes:
        parts.append(f"{len(non_additive_changes)} changement(s) d'additivite")
    if not parts:
        summary = "Aucun changement de structure de mapping." if unchanged else (
            "Mapping equivalent (aucun champ modifie)."
        )
    else:
        summary = "Diff de mapping : " + ", ".join(parts) + "."

    return {
        "before": before_meta,
        "after": after_meta,
        "unchanged": unchanged,
        "schema_changed": schema_changed,
        "added_fields": added,
        "removed_fields": removed,
        "type_changes": type_changes,
        "target_rebindings": target_rebindings,
        "role_changes": role_changes,
        "currency_conflicts": currency_conflicts,
        "non_additive_changes": non_additive_changes,
        "summary": summary,
    }
