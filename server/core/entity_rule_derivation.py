"""Business rules versioned on entity types: derived attributes (Story 68.6).

A declared entity type (Story 68.1: a registry IS the type) may carry a RULE
SET that derives classifications for its nodes -- ``content_type`` from a
duration, a tier from a size. The set mounts on the generic
:mod:`core.governance_rule_sets` lifecycle (Story 48.3/49.4), exactly as Money
Policy, FX and Timezone do: one family, one profile, no parallel engine. This
module owns what the generic lifecycle deliberately does not: the payload
schema, the Python-side evaluation, and the storage of what a publish derived.

THREE PROPERTIES THE WHOLE DESIGN SERVES.

* **A derived value can always name its maker (AC2).** Publishing a version
  computes the derived attributes for the registry's nodes and stores them in
  ``app.master_data_derived_attributes``, every row stamped with the rule-set
  version id that produced it. A reader never sees a classification without
  the version that wrote it.

* **Re-derivation never rewrites a fact (AC3, CAP-4).** The new version writes
  NEW stamped rows; the old ones stay, because an old Result may pin them.
  Node versions -- the facts the rules READ -- are never touched. A read
  resolves "the current published rule-set version" at read time and picks the
  rows stamped with it, so re-publishing changes what reads derive and nothing
  that was stored.

* **Publish fails closed (AC4).** An invalid set -- an input field the entity
  type's ``property_schema`` does not declare, an output name that collides
  with a published canonical attribute of the same kind -- is refused BEFORE
  anything is written, with a named reason. The derived rows and the pointer
  move live on the caller's one transaction, so nothing half-published exists.

Operators are evaluated in Python, never interpolated into SQL -- the
precedent ``core.business_alerts`` states at its line 50. The rule payload
carries field names and operator tokens; the database only ever sees
parameters.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from ulid import ULID

from core.audit import declare_action, insert_audit_row
from core.governance_rule_sets import (
    PUBLISHED,
    RuleSetConflict,
    RuleSetError,
    RuleSetNotFound,
    RuleSetProfile,
    RuleSetUnavailable,
    active_version,
    draft_version,
    ensure_rule_set,
    fetch_rule_set,
    publish_version,
    register_profile,
    require_version,
)
from core.master_data import current_node_versions, fetch_registry, list_nodes

FAMILY_ENTITY_DERIVATION = "entity_derivation"
PROFILE_ENTITY_DERIVATION = "entity_derivation_v1"

#: What this module writes to the audit trail (AD-42: declared where written).
ACTION_ENTITY_DERIVATION_PUBLISHED = declare_action("mdm.entity_derivation.published")

_OBJECT_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
_ATTRIBUTE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# ---------------------------------------------------------------------------
# Operator evaluation -- Python-only, never interpolated into SQL (the
# `core.business_alerts` precedent). A token outside this whitelist is a
# refusal at draft time, which is where "unparseable expression" is caught.
# ---------------------------------------------------------------------------

OPERATORS: dict[str, Any] = {
    "=": lambda fact, value: fact == value,
    "!=": lambda fact, value: fact != value,
    ">": lambda fact, value: fact > value,
    ">=": lambda fact, value: fact >= value,
    "<": lambda fact, value: fact < value,
    "<=": lambda fact, value: fact <= value,
    "in": lambda fact, values: fact in values,
}

_SCALAR_TYPES = (str, int, float, bool)

#: Sentinel for "no rule matched and no otherwise was declared": the attribute
#: derives NOTHING for this node, and no row is written. Distinct from a
#: derived value, so a reader never confuses "unclassified" with a value.
NO_MATCH: Any = object()


# ---------------------------------------------------------------------------
# The named publish refusals (AC4). Each carries a code a door can render.
# ---------------------------------------------------------------------------


class EntityTypeNotDeclared(RuleSetError):
    """The rule set derives on a kind no registry owns in this Project."""

    code = "entity_type_not_declared"


class NoTypeVersion(RuleSetError):
    """No type version, so no property_schema an input field can be checked against."""

    code = "no_type_version"


class UnknownInputField(RuleSetError):
    """A rule reads a field the entity type's property_schema does not declare."""

    code = "unknown_input_field"


class OutputAttributeConflict(RuleSetError):
    """A derived name collides with a published canonical attribute of the kind."""

    code = "output_attribute_conflict"


# ---------------------------------------------------------------------------
# Payload validation. Pure, so it is provable without a database; it is the
# profile the generic lifecycle calls at draft time, so a malformed expression
# never reaches a version row.
# ---------------------------------------------------------------------------


def _object_kind(value: Any) -> str:
    text = str(value or "").strip()
    if not _OBJECT_KIND_RE.fullmatch(text):
        raise RuleSetError("object_kind must be lowercase snake_case, 2 to 40 characters")
    return text


def _attribute_name(value: Any, label: str = "name") -> str:
    text = str(value or "").strip()
    if not _ATTRIBUTE_NAME_RE.fullmatch(text):
        raise RuleSetError(f"{label} must be lowercase snake_case, 1 to 64 characters")
    return text


def _scalar(value: Any, label: str) -> Any:
    if value is None or not isinstance(value, _SCALAR_TYPES):
        raise RuleSetError(f"{label} must be a string, a number or a boolean")
    return value


def _validate_rule(spec: Mapping[str, Any], *, attribute: str) -> dict[str, Any]:
    """One normalized rule, or the refusal naming what is malformed.

    A rule is ``when`` (field, operator, compared value) and ``then`` (the
    output the match derives). The compared value of an ``in`` rule is a
    non-empty list of scalars; every other operator compares against one
    scalar. ``None`` is refused everywhere: a missing FACT already never
    matches, so comparing against nothing would say nothing.
    """

    if not isinstance(spec, Mapping):
        raise RuleSetError(f"rules of {attribute!r} must be objects")
    when = spec.get("when")
    if not isinstance(when, Mapping):
        raise RuleSetError(f"a rule of {attribute!r} must carry a 'when' object")
    field = _attribute_name(when.get("field"), "when.field")
    op = str(when.get("op") or "").strip()
    if op not in OPERATORS:
        raise RuleSetError(
            f"unknown operator {op!r} in attribute {attribute!r} -- "
            f"expected one of {sorted(OPERATORS)}"
        )
    if op == "in":
        values = when.get("value")
        if not isinstance(values, (list, tuple)) or not values:
            raise RuleSetError(f"an 'in' rule of {attribute!r} needs a non-empty list of values")
        value: Any = [_scalar(item, f"when.value of {attribute!r}") for item in values]
    else:
        value = _scalar(when.get("value"), f"when.value of {attribute!r}")
    then = _scalar(spec.get("then"), f"then of {attribute!r}")
    return {"when": {"field": field, "op": op, "value": value}, "then": then}


def validate_derived_attribute(spec: Mapping[str, Any]) -> dict[str, Any]:
    """One normalized derived-attribute declaration: name, ordered rules, fallback.

    Rule ORDER is content -- first match wins at evaluation -- so the rules are
    normalized in place and never sorted. ``otherwise`` is optional: without
    it, a node no rule matches derives nothing for this attribute.
    """

    if not isinstance(spec, Mapping):
        raise RuleSetError("a derived attribute must be an object")
    name = _attribute_name(spec.get("name"))
    rules = spec.get("rules")
    if not isinstance(rules, (list, tuple)) or not rules:
        raise RuleSetError(f"derived attribute {name!r} must declare at least one rule")
    normalized: dict[str, Any] = {
        "name": name,
        "rules": [_validate_rule(rule, attribute=name) for rule in rules],
    }
    if "otherwise" in spec and spec.get("otherwise") is not None:
        normalized["otherwise"] = _scalar(spec.get("otherwise"), f"otherwise of {name!r}")
    return normalized


def _validate_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The profile validator the generic lifecycle calls at draft time.

    Normalizes, so two logically identical payloads hash identically and "has
    the rule set changed?" stays answerable. A second attribute of the same
    name is a second authority over one output, and is refused.
    """

    if not isinstance(payload, Mapping):
        raise RuleSetError("an entity derivation payload must be an object")
    kind = _object_kind(payload.get("object_kind"))
    declared = payload.get("derived_attributes")
    if not isinstance(declared, (list, tuple)) or not declared:
        raise RuleSetError("a rule set must declare at least one derived attribute")
    attributes = [validate_derived_attribute(spec) for spec in declared]
    names = [attribute["name"] for attribute in attributes]
    if len(set(names)) != len(names):
        raise RuleSetError("two derived attributes cannot carry the same name")
    return {"object_kind": kind, "derived_attributes": attributes}


register_profile(
    RuleSetProfile(
        key=PROFILE_ENTITY_DERIVATION,
        family=FAMILY_ENTITY_DERIVATION,
        label="Entity Derivation",
        validate=_validate_payload,
    )
)


# ---------------------------------------------------------------------------
# Evaluation. Pure: facts in, derived value (or NO_MATCH) out.
# ---------------------------------------------------------------------------


def evaluate_attribute(attribute: Mapping[str, Any], facts: Mapping[str, Any]) -> Any:
    """The value the rules derive for one node, or :data:`NO_MATCH`.

    A fact that is ABSENT matches nothing -- not even ``!=``: "unknown" is not
    "different". A fact of an incomparable type (a string against a numeric
    bound) matches nothing either: the type_mismatch half of data quality is
    the attributes view's job (migration 241), and a crash here would refuse a
    publish for one bad node. First matching rule wins; otherwise the declared
    fallback; otherwise NO_MATCH, and no row is written for the pair.

    ``otherwise`` IS A FALLBACK, NEVER AN ASSERTION ABOUT A NODE THE RULES
    CANNOT SEE. It answers "the facts are known and none of the rules fired";
    it does not answer "this node carries none of the facts the rules read".
    A node whose payload holds NO field any rule of this attribute references
    derives NOTHING -- unclassified is the ABSENCE of a stamped row (AD-9),
    never a fallback value stored as if it had been measured. Stamping the
    fallback there would let a catalogue with a missing column silently
    classify every one of its entities.
    """

    seen_any_input = False
    for rule in attribute.get("rules") or ():
        when = rule["when"]
        fact = (facts or {}).get(when["field"])
        if fact is None:
            continue
        seen_any_input = True
        try:
            if OPERATORS[when["op"]](fact, when["value"]):
                return rule["then"]
        except TypeError:
            continue
    if not seen_any_input:
        return NO_MATCH
    return attribute.get("otherwise", NO_MATCH)


# ---------------------------------------------------------------------------
# Draft and publish, mounted on the generic lifecycle. Callers own the
# transaction: the derived rows, the audit evidence and the pointer move
# commit together or not at all.
# ---------------------------------------------------------------------------


def draft_entity_rule_set(
    conn,
    *,
    org_id: str,
    project_id: str,
    object_kind: str,
    derived_attributes: Sequence[Mapping[str, Any]],
    label: str,
    actor: str,
    description: str | None = None,
) -> dict[str, Any]:
    """Draft the next version of the derivation rule set of ONE entity type.

    One head per (Project, entity type): the head's ``name`` IS the object
    kind, because a second rule set over the same type would be a second
    authority over the same classifications. The payload's shape is validated
    by the profile here; the checks that need the database (input fields,
    output collisions) are publish-time refusals, per AC4.
    """

    kind = _object_kind(object_kind)
    head = ensure_rule_set(
        conn,
        org_id=org_id,
        project_id=project_id,
        family=FAMILY_ENTITY_DERIVATION,
        name=kind,
        label=label,
        actor=actor,
    )
    return draft_version(
        conn,
        project_id=project_id,
        rule_set_id=str(head["id"]),
        profile=PROFILE_ENTITY_DERIVATION,
        label=label,
        payload={"object_kind": kind, "derived_attributes": list(derived_attributes)},
        actor=actor,
        description=description,
    )


def publish_entity_rule_set(
    conn, *, project_id: str, rule_set_id: str, version_id: str, actor: str
) -> dict[str, Any]:
    """Validate, derive, stamp -- and only then make the version current.

    ORDER IS THE FAIL-CLOSED HALF OF AC4. Every check runs BEFORE any write;
    the derived rows and the audit row are written on the caller's transaction
    BEFORE the generic ``publish_version`` moves the pointer, so a refusal
    leaves no trace and a failure anywhere leaves nothing half-published. A
    replay (the version is already current) writes nothing and returns it.
    """

    head = fetch_rule_set(conn, project_id=project_id, rule_set_id=rule_set_id)
    if head is None:
        raise RuleSetNotFound(f"rule set {rule_set_id} is not in this Project")
    if head["family"] != FAMILY_ENTITY_DERIVATION:
        raise RuleSetConflict(
            f"rule set {rule_set_id} belongs to family {head['family']!r}, "
            f"not {FAMILY_ENTITY_DERIVATION!r}"
        )
    version = require_version(conn, project_id=project_id, version_id=version_id)
    if version["rule_set_id"] != rule_set_id:
        raise RuleSetConflict("that version belongs to another rule set")
    if version["status"] == PUBLISHED:
        return version

    payload = version["payload"] or {}
    object_kind = str(payload.get("object_kind") or "")
    derived = payload.get("derived_attributes") or []

    registry = fetch_registry(conn, project_id=project_id, object_kind=object_kind)
    if registry is None:
        raise EntityTypeNotDeclared(
            f"no entity type is declared for object kind {object_kind!r} in this "
            "Project -- declare it (68.1) before publishing rules that derive on it"
        )

    known_fields = _known_input_fields(
        conn, org_id=str(registry["org_id"]), object_kind=object_kind
    )
    used_fields = sorted(
        {rule["when"]["field"] for attribute in derived for rule in attribute["rules"]}
    )
    unknown = [field for field in used_fields if field not in known_fields]
    if unknown:
        raise UnknownInputField(
            f"input field(s) {', '.join(unknown)} are not declared by the property "
            f"schema of {object_kind!r}'s type version -- declare them there, or "
            f"remove the rule that reads them (known: {', '.join(sorted(known_fields)) or 'none'})"
        )

    collisions = sorted(
        _published_attribute_names(conn, project_id=project_id, object_kind=object_kind)
        & {attribute["name"] for attribute in derived}
    )
    if collisions:
        raise OutputAttributeConflict(
            f"derived attribute(s) {', '.join(collisions)} collide with a published "
            f"canonical attribute of {object_kind!r} -- a governed attribute and a "
            "derived one cannot answer the same name"
        )

    written = _derive_and_stamp(
        conn,
        project_id=project_id,
        rule_set_id=rule_set_id,
        version_id=version_id,
        registry=registry,
        derived=derived,
        actor=actor,
    )
    insert_audit_row(
        conn,
        identity=actor or "anonymous",
        action=ACTION_ENTITY_DERIVATION_PUBLISHED,
        provider_account="",
        connection_ref="",
        metadata={
            "project_id": project_id,
            "rule_set_id": rule_set_id,
            "rule_set_version_id": version_id,
            "object_kind": object_kind,
            "registry_id": str(registry["id"]),
            "derived_attributes": [attribute["name"] for attribute in derived],
            "values_written": written,
        },
    )
    return publish_version(
        conn, project_id=project_id, rule_set_id=rule_set_id, version_id=version_id, actor=actor
    )


# ---------------------------------------------------------------------------
# The read path. The version is resolved at READ time: re-publishing changes
# what this returns, never what was stored.
# ---------------------------------------------------------------------------


def resolve_derived_attributes(
    conn,
    *,
    project_id: str,
    object_kind: str,
    node_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """The derived attributes of one entity type, stamped with their version.

    Resolves the CURRENT published rule-set version of the type's head and
    returns the rows it produced -- each under its node, each carrying the
    version id that wrote it, so a reader can always name which version
    produced a value (AC2).

    Fail-closed: with no published rule set there is no version to name, and
    inventing classifications would be exactly the implicit default the
    lifecycle exists to refuse -- so this raises :class:`RuleSetUnavailable`.
    An unreadable store raises through the same way (never an empty answer).
    """

    kind = _object_kind(object_kind)
    found = active_version(conn, project_id=project_id, family=FAMILY_ENTITY_DERIVATION, name=kind)
    if found is None:
        raise RuleSetUnavailable(
            f"no published rule set derives attributes for object kind {kind!r} in "
            "this Project -- a classification without a version to name it is not "
            "a classification"
        )
    head, version = found
    version_id = str(version["id"])

    clauses = ["project_id = %s", "rule_set_version_id = %s"]
    params: list[Any] = [project_id, version_id]
    if node_ids is not None:
        if not node_ids:
            return _resolved(head, version, {})
        clauses.append("node_id = ANY(%s)")
        params.append(list(node_ids))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT node_id, attribute, value FROM app.master_data_derived_attributes "
            f"WHERE {' AND '.join(clauses)} ORDER BY node_id, attribute",
            params,
        )
        rows = cur.fetchall()

    nodes: dict[str, dict[str, Any]] = {}
    for node_id, attribute, value in rows:
        # `value` is JSONB (migration 294) and the driver already decoded it:
        # a stored JSON string comes back as a Python str, a number as a
        # number. Re-parsing a str here would try to read `short` as a JSON
        # document and raise -- the exact defect this line used to carry.
        nodes.setdefault(str(node_id), {})[str(attribute)] = value
    return _resolved(head, version, nodes)


def _resolved(
    head: Mapping[str, Any], version: Mapping[str, Any], nodes: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    return {
        "rule_set_id": str(head["id"]),
        "rule_set_version_id": str(version["id"]),
        "content_hash": str(version["content_hash"]),
        "nodes": nodes,
    }


# ---------------------------------------------------------------------------
# Internal: the publish-time reads and the stamped write.
# ---------------------------------------------------------------------------


def _known_input_fields(conn, *, org_id: str, object_kind: str) -> set[str]:
    """The properties the entity type's CURRENT type version declares.

    "Current" is the highest version number of the kind's type versions: type
    versions are content-addressed and immutable, and the latest one is the
    definition nodes are now written against. No type version at all is a
    refusal, not an empty check -- a rule whose inputs nothing declares would
    publish looking complete while reading fields that exist nowhere.
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT property_schema FROM app.master_data_type_versions
             WHERE org_id = %s AND object_kind = %s
             ORDER BY version_number DESC LIMIT 1
            """,
            (org_id, object_kind),
        )
        row = cur.fetchone()
    if row is None:
        raise NoTypeVersion(
            f"object kind {object_kind!r} has no type version, so no property schema "
            "declares the fields a rule may read -- declare the type version first"
        )
    schema = row[0]
    if isinstance(schema, str):  # pragma: no cover - driver-dependent
        schema = json.loads(schema)
    properties = (schema or {}).get("properties") or {}
    return {str(name) for name in properties}


def _published_attribute_names(conn, *, project_id: str, object_kind: str) -> set[str]:
    """The canonical attributes already published for this kind (migration 241)."""

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT canonical_name FROM app.mdm_canonical_fields
             WHERE project_id = %s AND object_kind = %s AND status = 'active'
            """,
            (project_id, object_kind),
        )
        return {str(row[0]) for row in cur.fetchall()}


def _derive_and_stamp(
    conn,
    *,
    project_id: str,
    rule_set_id: str,
    version_id: str,
    registry: Mapping[str, Any],
    derived: Sequence[Mapping[str, Any]],
    actor: str,
) -> int:
    """Compute every derived attribute of every live node and stamp the rows.

    Reads the CURRENT node version's ``attributes`` -- the governed facts --
    and never writes back to them. Nodes without a current version carry no
    facts and derive nothing. The unique index on (project, node, attribute,
    version) makes a replayed insert a no-op, so a retried publish cannot
    double a value.
    """

    nodes = list_nodes(conn, project_id=project_id, registry_id=str(registry["id"]))
    currents = current_node_versions(
        conn, org_id=str(registry["org_id"]), registry_id=str(registry["id"])
    )
    written = 0
    with conn.cursor() as cur:
        for node in nodes:
            current = currents.get(str(node["id"]))
            payload = (current or {}).get("payload") or {}
            if isinstance(payload, str):  # pragma: no cover - driver-dependent
                payload = json.loads(payload)
            facts = payload.get("attributes") or {}
            for attribute in derived:
                value = evaluate_attribute(attribute, facts)
                if value is NO_MATCH:
                    continue
                cur.execute(
                    """
                    INSERT INTO app.master_data_derived_attributes
                        (id, org_id, project_id, registry_id, node_id, attribute, value,
                         rule_set_id, rule_set_version_id, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        f"mdder_{ULID()}",
                        str(registry["org_id"]),
                        project_id,
                        str(registry["id"]),
                        str(node["id"]),
                        attribute["name"],
                        json.dumps(value),
                        rule_set_id,
                        version_id,
                        actor or "anonymous",
                    ),
                )
                written += cur.rowcount
    return written
