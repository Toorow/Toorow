"""toorow -- Declarative pre-call field-compatibility engine (Story 26.1, B).

Generalises the per-module compatibility mechanics (a breakdown-pair matrix
here, a group-alone dimension there) into ONE core engine driven by a
versioned, declarative rule block. The rules live in each module's
``catalog_sources/catalog_sources.json`` under the ``field_compatibility``
key; core knows only the schema (AD-2: zero provider vocabulary in this file).

Contract (schema: core/schemas/field-compatibility.schema.json, version "1"):

    {
      "schema_version": "1",
      "rules": [
        {"id": "...", "kind": "mutually_exclusive", "sets": [[..], [..]]},
        {"id": "...", "kind": "requires", "field": "x",
         "requires_fields": ["y"]},
        {"id": "...", "kind": "selectable_set",
         "scope": {"<context_key>": "<value>", ...},
         "allowed_fields": [..]},
        {"id": "...", "kind": "only_alone", "field": "x"},
        {"id": "...", "kind": "max_selection", "limit": N, "fields": [..]}
      ]
    }

Rule semantics:
  mutually_exclusive  selecting fields from MORE THAN ONE of the declared
                      sets is a violation (impression-share vs attribute
                      column families, incompatible breakdown pairs, ...).
  requires            when ``field`` is selected, every field in
                      ``requires_fields`` must also be selected.
  selectable_set      applies only when EVERY scope key/value pair matches
                      the caller-provided ``context`` (e.g. a resource or a
                      report_type x group_by pair); then every selected field
                      must belong to ``allowed_fields``. With a NON-MATCHING
                      context the rule is inert. A MISSING context (None/{})
                      while selectable_set rules exist is a caller bug and
                      raises ValueError unless allow_missing_context=True
                      (review 26.1 F-6).
  only_alone          the field is incompatible with any other selected field.
  max_selection       the selection (optionally intersected with ``fields``)
                      may not exceed ``limit`` entries.

Exports:
  FIELD_COMPATIBILITY_KEY            -- the catalog_sources.json block key
  CompatViolation                    -- one deterministic violation record
  FieldCompatibilityError            -- loud load-time failure (malformed rules)
  validate_rules(rules) -> list[str] -- schema+semantic errors ([] when valid)
  load_rules(rules) -> dict          -- validated rules or raises (loud)
  load_module_rules(module_dir)      -- loader hook: read + validate the block
  validate_selection(selection, rules, *, context=None,
      allow_missing_context=False) -> list[CompatViolation]
  enforce_selection(selection, rules, *, context=None,
      allow_missing_context=False)                       -- violations =>
      typed core.pull_errors.InvalidRequestError BEFORE any API call, with
      the rule id(s) in the message and the full violation list in the
      error's provider_payload (=> persisted in the job's error_detail).

Backward compatibility: existing module-local mechanisms keep working
untouched -- this engine only activates for modules that OPT IN by declaring
a ``field_compatibility`` block WITH the ``schema_version`` marker. A block
without the marker is a module-local companion format (consumed by the
module's own code) and is ignored by the core engine (no-op, like an absent
block). ASCII-only strings (AI-03).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import jsonschema
from referencing import Registry, Resource

from core.pull_errors import InvalidRequestError

logger = logging.getLogger(__name__)

FIELD_COMPATIBILITY_KEY = "field_compatibility"

_SCHEMA_PATH = Path(__file__).parent / "schemas" / "field-compatibility.schema.json"
_SCHEMA_ID = "https://toorow.dev/schemas/field-compatibility.schema.json"

# Location of a module's rule block, relative to the module directory.
_CATALOG_SOURCES_RELPATH = Path("catalog_sources") / "catalog_sources.json"

_schema_cache: dict | None = None


class FieldCompatibilityError(ValueError):
    """Malformed field_compatibility rules -- raised LOUDLY at module load."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True, order=True)
class CompatViolation:
    """One deterministic, pre-call compatibility violation."""

    rule_id: str
    kind: str
    message: str
    fields: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "kind": self.kind,
            "message": self.message,
            "fields": list(self.fields),
        }


# ---------------------------------------------------------------------------
# Rule validation (loud at module load -- never a silent degradation)
# ---------------------------------------------------------------------------


def _load_schema() -> dict:
    global _schema_cache
    if _schema_cache is None:
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        _schema_cache = schema
    return _schema_cache


def _validator() -> jsonschema.Draft202012Validator:
    schema = _load_schema()
    registry = Registry().with_resource(schema["$id"], Resource.from_contents(schema))
    return jsonschema.Draft202012Validator({"$ref": _SCHEMA_ID}, registry=registry)


def validate_rules(rules) -> list[str]:
    """Return every contract violation in *rules* ([] when valid).

    Two stages: JSON Schema validation against the versioned contract, then
    semantic checks the schema cannot express (unique rule ids; disjoint
    mutually_exclusive sets -- review 26.1 F-5). Deterministic (sorted paths)
    so a failing load always prints the same report.
    """
    errors = [
        (
            "$"
            + ("" if not e.absolute_path else "." + ".".join(str(p) for p in e.absolute_path))
            + ": "
            + e.message
        )
        for e in sorted(
            _validator().iter_errors(rules),
            key=lambda e: (tuple(str(p) for p in e.absolute_path), e.message),
        )
    ]
    if errors:
        return errors

    seen: set[str] = set()
    for index, rule in enumerate(rules.get("rules", [])):
        rid = rule.get("id")
        if rid in seen:
            errors.append(f"$.rules: duplicate rule id {rid!r}")
        seen.add(rid)

        # Review 26.1 F-5: within one mutually_exclusive rule the declared
        # sets must be pairwise disjoint -- a field in two sets makes the
        # rule self-contradictory (selecting it alone would already touch
        # two sets), which is a declaration mistake, refused like a
        # duplicate id.
        if rule.get("kind") == "mutually_exclusive":
            counts: dict[str, int] = {}
            for field_set in rule.get("sets", []):
                for field_id in set(field_set):
                    counts[field_id] = counts.get(field_id, 0) + 1
            shared = sorted(f for f, n in counts.items() if n > 1)
            if shared:
                errors.append(
                    f"$.rules.{index}: mutually_exclusive rule {rid!r} sets "
                    "must be disjoint; field(s) in more than one set: "
                    + ", ".join(shared)
                )
    return errors


def load_rules(rules) -> dict:
    """Validate *rules* and return them; raise FieldCompatibilityError (loud)."""
    errors = validate_rules(rules)
    if errors:
        raise FieldCompatibilityError(errors)
    return rules


def load_module_rules(module_dir: Path) -> dict | None:
    """Loader hook: read + validate a module's field_compatibility block.

    The core contract is OPT-IN and self-identifies via ``schema_version``:
    a ``field_compatibility`` block WITHOUT that marker is a module-local
    companion format (some connectors ship their own compatibility source
    under the same key, consumed by their own code -- e.g. a per-resource
    matrix predating this engine). Such blocks are treated as undeclared
    (no-op): the module's local mechanism keeps working untouched, exactly
    like modules with no block at all.

    Returns:
        The validated rules dict, or None when the module declares no core
        contract block (absent catalog_sources.json, absent block, or a
        module-local block without ``schema_version``) -- the backward-
        compatible no-op for every existing module.

    Raises:
        FieldCompatibilityError: unparseable catalog_sources.json or a
        malformed OPTED-IN block (``schema_version`` present). The loader
        turns this into a loud module skip -- a malformed contract must
        never load as if it were valid.
    """
    sources_path = Path(module_dir) / _CATALOG_SOURCES_RELPATH
    if not sources_path.is_file():
        return None
    try:
        sources = json.loads(sources_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FieldCompatibilityError(
            [f"catalog_sources.json is unreadable or not valid JSON: {exc}"]
        ) from exc
    if not isinstance(sources, dict):
        return None
    block = sources.get(FIELD_COMPATIBILITY_KEY)
    if block is None:
        return None
    if not isinstance(block, dict) or "schema_version" not in block:
        logger.info(
            "field_compat: module-local field_compatibility block without "
            "schema_version -- ignored by the core engine (dir=%s)",
            module_dir,
        )
        return None
    return load_rules(block)


# ---------------------------------------------------------------------------
# Selection validation (pure -- no I/O)
# ---------------------------------------------------------------------------


def _selection_ids(selection) -> list[str]:
    """Normalise a selection into a flat, order-preserving list of field ids.

    Accepts either the catalog_contract selection dict
    ``{"metrics": [...], "dimensions": [...]}`` or any iterable of strings.
    """
    if isinstance(selection, dict):
        merged: list[str] = []
        merged.extend(selection.get("metrics") or [])
        merged.extend(selection.get("dimensions") or [])
        return [f for f in merged if isinstance(f, str) and f]
    if selection is None:
        return []
    return [f for f in selection if isinstance(f, str) and f]


def _scope_matches(scope: dict, context: dict | None) -> bool:
    """True when every scope key/value pair is satisfied by *context*."""
    if not context:
        return False
    return all(context.get(key) == value for key, value in scope.items())


def validate_selection(
    selection, rules, *, context=None, allow_missing_context=False
) -> list[CompatViolation]:
    """Evaluate every rule against *selection* and return the violations.

    Args:
        selection: {"metrics": [...], "dimensions": [...]} or iterable of ids.
        rules:     A VALIDATED field_compatibility dict (load_rules output --
                   callers must not feed unvalidated rules here).
        context:   Opaque scope values for selectable_set rules,
                   e.g. {"resource": "..."} or {"report_type": "...",
                   "group_by": "..."}. Keys/values are opaque to core (AD-2).
        allow_missing_context:
                   Review 26.1 F-6 -- STRICT BY DEFAULT: when *rules* declare
                   at least one ``selectable_set`` rule and *context* is
                   None/{}, this function raises ValueError (a caller bug:
                   the scoped rules would silently be inert and an invalid
                   selection would reach the provider). Pass True only for
                   the documented legitimate cases where no scope exists yet
                   (e.g. a scope-agnostic catalog lint of the selection
                   before the resource/report_type is chosen).

    Returns:
        Deterministic (sorted) list of CompatViolation; empty when compatible.

    Raises:
        ValueError: missing context while the rules contain selectable_set
            scopes and ``allow_missing_context`` is False (caller bug -- NOT
            a selection violation, hence not a CompatViolation).
    """
    if not allow_missing_context and not context:
        scoped = [
            r.get("id") for r in rules.get("rules", []) if r.get("kind") == "selectable_set"
        ]
        if scoped:
            raise ValueError(
                "validate_selection: rules declare selectable_set scope(s) "
                + ", ".join(repr(r) for r in scoped)
                + " but no context was provided; pass the scope context or "
                "set allow_missing_context=True for the documented "
                "scope-less cases (review 26.1 F-6)"
            )

    selected_list = _selection_ids(selection)
    selected = set(selected_list)
    violations: list[CompatViolation] = []

    for rule in rules.get("rules", []):
        rid = rule["id"]
        kind = rule["kind"]

        if kind == "mutually_exclusive":
            touched: list[tuple[int, tuple[str, ...]]] = []
            for index, field_set in enumerate(rule["sets"]):
                hit = tuple(sorted(selected & set(field_set)))
                if hit:
                    touched.append((index, hit))
            if len(touched) > 1:
                offending = tuple(sorted({f for _, hit in touched for f in hit}))
                violations.append(
                    CompatViolation(
                        rule_id=rid,
                        kind=kind,
                        message=(
                            f"rule {rid!r}: fields from {len(touched)} mutually "
                            "exclusive sets were selected together: "
                            + ", ".join(offending)
                        ),
                        fields=offending,
                    )
                )

        elif kind == "requires":
            trigger = rule["field"]
            if trigger in selected:
                missing = tuple(sorted(set(rule["requires_fields"]) - selected))
                if missing:
                    violations.append(
                        CompatViolation(
                            rule_id=rid,
                            kind=kind,
                            message=(
                                f"rule {rid!r}: field {trigger!r} requires "
                                "missing field(s): " + ", ".join(missing)
                            ),
                            fields=missing,
                        )
                    )

        elif kind == "selectable_set":
            if _scope_matches(rule["scope"], context):
                outside = tuple(sorted(selected - set(rule["allowed_fields"])))
                if outside:
                    violations.append(
                        CompatViolation(
                            rule_id=rid,
                            kind=kind,
                            message=(
                                f"rule {rid!r}: field(s) not selectable in this "
                                "scope: " + ", ".join(outside)
                            ),
                            fields=outside,
                        )
                    )

        elif kind == "only_alone":
            lone = rule["field"]
            if lone in selected and len(selected) > 1:
                others = tuple(sorted(selected - {lone}))
                violations.append(
                    CompatViolation(
                        rule_id=rid,
                        kind=kind,
                        message=(
                            f"rule {rid!r}: field {lone!r} cannot be combined "
                            "with: " + ", ".join(others)
                        ),
                        fields=others,
                    )
                )

        elif kind == "max_selection":
            pool_fields = rule.get("fields")
            pool = selected & set(pool_fields) if pool_fields else selected
            limit = rule["limit"]
            if len(pool) > limit:
                offending = tuple(sorted(pool))
                violations.append(
                    CompatViolation(
                        rule_id=rid,
                        kind=kind,
                        message=(
                            f"rule {rid!r}: {len(pool)} fields selected, "
                            f"maximum is {limit}: " + ", ".join(offending)
                        ),
                        fields=offending,
                    )
                )

    return sorted(violations)


def enforce_selection(selection, rules, *, context=None, allow_missing_context=False) -> None:
    """Refuse an incompatible selection BEFORE any API call (typed, AD-2).

    Raises core.pull_errors.InvalidRequestError (error_class
    ``invalid_request``, non-retryable) carrying the violating rule id(s) in
    the message and the full violation list in provider_payload -- the worker
    serialises provider_payload into the job's error_detail, so the rule id
    is preserved end-to-end. No-op when the selection is compatible.

    ``allow_missing_context`` follows the validate_selection contract (review
    26.1 F-6): missing context with selectable_set rules raises ValueError.
    """
    violations = validate_selection(
        selection, rules, context=context, allow_missing_context=allow_missing_context
    )
    if not violations:
        return
    rule_ids = sorted({v.rule_id for v in violations})
    raise InvalidRequestError(
        provider_status=None,
        provider_payload={"violations": [v.as_dict() for v in violations]},
        message=(
            "field compatibility violation (pre-call refusal): rules="
            + ",".join(rule_ids)
            + "; "
            + "; ".join(v.message for v in violations)
        ),
    )
