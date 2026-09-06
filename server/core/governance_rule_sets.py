"""The generic Governance Rule Set lifecycle (Story 48.3, in the Story 49.4 shape).

Story 49.4 owns Controls & Quality and specifies ``governance_rule_sets`` /
``governance_rule_set_versions`` / ``rule_set_exceptions`` with "stable head,
immutable complete content, family, exact scope and active/pending/LKG". It has
not landed. Story 48.3's Implementation Gate forbids building "a temporary
currency-specific or timezone-specific Governance lifecycle and calling it
complete" -- so this module is the generic lifecycle, and Money Policy, FX
ingestion and Timezone Policy are three *families* mounted on it, exactly as
Country mounts on :mod:`core.master_data`.

Nothing here knows what a currency or a timezone is. It knows:

* a **rule set** -- one stable head per (Project, family, name), with three
  distinct version pointers, because a failed publication that moves ``current``
  is indistinguishable from one that worked;
* a **version** -- editable while ``draft``, frozen the moment it is published,
  carrying its complete typed content plus the exact owner versions it depends
  on (``requires``). "The latest version of X" is not a dependency;
* a **profile** -- a named validator a family registers. The profile owns the
  payload schema, the ordering rule and the refusal reasons. This module calls
  it and never inspects a payload key itself;
* an **exception** -- a version-bound, interval-bound, reasoned exemption. An
  exception with no interval is a permanent silent hole, so the schema requires
  one.

Callers own the transaction: a mutation and its audit evidence commit together.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Mapping, Sequence

from ulid import ULID

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,126}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_FAMILY_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

LIFECYCLE_STATUSES = ("draft", "candidate", "published", "superseded", "archived")
#: The one status a consumer may read as authoritative.
PUBLISHED = "published"


class RuleSetError(ValueError):
    """A governed Rule Set operation was rejected."""

    code = "invalid_rule_set_operation"


class RuleSetNotFound(RuleSetError):
    """The addressed rule set or version does not exist in this Project."""

    code = "rule_set_not_found"


class RuleSetConflict(RuleSetError):
    """The operation contradicts an invariant the model guarantees."""

    code = "rule_set_conflict"


class RuleSetUnavailable(RuntimeError):
    """Evidence required to decide is unreadable; the caller must fail closed.

    Deliberately not a :class:`RuleSetError` subclass, so a caller catching
    invalid input does not also swallow an outage as "no policy" -- which is how
    an unreadable policy becomes an implicit default.
    """

    code = "rule_set_evidence_unavailable"


# ---------------------------------------------------------------------------
# Profiles. A family registers one; this module never reads a payload key.
# ---------------------------------------------------------------------------

#: The answer shapes a declared field admits. Deliberately small: every one of
#: them is a control the console already knows how to render, so adding a family
#: never adds a widget.
FORM_FIELD_KINDS = ("choice", "text", "integer", "decimal", "boolean", "text_list")


@dataclass(frozen=True, slots=True)
class RuleSetFormOption:
    """One admissible answer, and what choosing it costs.

    ``consequence`` is stated where the answer is chosen rather than in a note
    under the form: the ladder adoption learned that the hard way, and a policy
    answered without knowing what it forbids is answered by shape.
    """

    value: str
    label: str
    consequence: str | None = None


@dataclass(frozen=True, slots=True)
class RuleSetFormField:
    """One thing a version of this family decides, in the operator's words.

    Declared BY THE FAMILY, next to the validator that refuses a wrong answer --
    the same rule ``governed_dependencies`` follows. The console renders what is
    declared here and holds no knowledge of any family: a field list written in
    the front end would be a second place to state what a version contains, free
    to disagree with the validator that judges it.

    ``key`` is a payload key of the profile's NORMALIZED output, so what is
    offered and what is stored cannot drift apart.
    """

    key: str
    #: The question, as a person is asked it. Never a column name.
    question: str
    kind: str = "text"
    options: tuple[RuleSetFormOption, ...] = ()
    #: What the answer decides, one sentence. Rendered under the question.
    why: str | None = None
    required: bool = True
    #: The unit an integer or decimal is counted in ("days", "fraction").
    unit: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in FORM_FIELD_KINDS:
            raise RuleSetError(f"a form field kind must be one of {list(FORM_FIELD_KINDS)}")
        if self.kind == "choice" and not self.options:
            raise RuleSetError(f"field {self.key!r} is a choice with no options")
        if self.kind != "choice" and self.options:
            raise RuleSetError(f"field {self.key!r} offers options but is not a choice")

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "question": self.question,
            "kind": self.kind,
            "why": self.why,
            "required": self.required,
            "unit": self.unit,
            "options": [
                {"value": option.value, "label": option.label, "consequence": option.consequence}
                for option in self.options
            ],
        }


@dataclass(frozen=True, slots=True)
class RuleSetProfile:
    """What a family must declare before its content can be published.

    ``validate`` receives the proposed payload and returns the NORMALIZED one, or
    raises :class:`RuleSetError`. Normalizing inside the profile is what makes the
    content hash meaningful: two logically identical payloads that differ only in
    key order must hash the same, or "has the policy changed?" is unanswerable.
    """

    key: str
    family: str
    label: str
    validate: Callable[[Mapping[str, Any]], dict[str, Any]]
    #: Owner-reference kinds this profile's ``requires`` entries may name.
    required_reference_kinds: tuple[str, ...] = ()
    #: Optional normalizer for the ORDERED RULES of a version.
    #:
    #: This module's contract says a profile owns "the payload schema, the
    #: ordering rule and the refusal reasons", but until Story 48.4 ``ordered_rules``
    #: reached the database as ``[dict(rule) for rule in ...]`` -- unvalidated,
    #: unordered and unnormalized. A family whose content IS an ordered ladder
    #: (Tax & Fees) could therefore publish an immutable version containing a rule
    #: with no jurisdiction, no effective date and no source, and the content hash
    #: would faithfully attest to it.
    #:
    #: Receives the proposed rules and returns the NORMALIZED, ORDERED list, or
    #: raises :class:`RuleSetError`. A family whose content is a single policy
    #: object (Money, FX, Timezone) leaves this ``None`` and rules stay empty.
    validate_rules: Callable[[Sequence[Mapping[str, Any]]], list[dict[str, Any]]] | None = None
    #: Optional declaration of the GOVERNED NODES this family's published content
    #: depends on (Story 37.9).
    #:
    #: The used-by guard on Country markets (``core.market_governance``) could
    #: already refuse a change that would silently re-mean a bound figure, and it
    #: had nothing to refuse: ``register_market_binding`` had NO production caller,
    #: so ``fetch_used_by`` truthfully returned an empty list and every market
    #: change was authorized. A guard whose input nobody writes is a guard that
    #: reports safety it never checked.
    #:
    #: A Tax & Fee ladder rule pinned to a market is the first real dependent, and
    #: it is the one that matters most: remove that market, or move a country out
    #: of it, and an already-published invoice figure means something else while
    #: every binding still resolves and looks healthy.
    #:
    #: Declared BY THE FAMILY, in its own module, rather than enumerated here: this
    #: file knows nothing about geography, and the next family that pins a governed
    #: node must not need an edit to a shared publisher.
    #:
    #: Pure. Receives ``(payload, ordered_rules)`` of the version being published
    #: and returns the dependencies it declares. Raising is a refusal to publish.
    governed_dependencies: (
        Callable[[Mapping[str, Any], Sequence[Mapping[str, Any]]], Sequence["GovernedDependency"]]
        | None
    ) = None
    #: What a version of this family decides, one declared field per answer.
    #:
    #: This is the whole authoring door (`governance.md`, "A Rule Set version is
    #: drafted, then published", 2026-08-24). Until then exactly two of the eight
    #: families could be written from the console -- `tax_fee` through preset
    #: adoption, `source_currency` through the mapping screen's conflict dialog --
    #: and the other six rendered a read-only panel whose empty state named no
    #: gesture that would fill it. Building the seventh screen for the seventh
    #: family is what `CLAUDE.md` forbids; declaring the questions here is the
    #: same repair made once for every family.
    #:
    #: Only the payload is offered. A family whose content is an ordered ladder
    #: keeps its own gesture for the RULES -- a ladder is proposed from governed
    #: presets, never typed -- and the version composed from this form carries the
    #: rules of the version in force forward untouched.
    form: tuple[RuleSetFormField, ...] = ()
    #: For a family whose version content is composed by ANOTHER governed gesture:
    #: the sentence naming it, printed where the version is read.
    #:
    #: `source_currency` is the case. Its entire content is the list of
    #: declarations, and declaring or withdrawing one on the mapping screen
    #: already republishes the set as a new version. A second editor here would be
    #: a second place to write one fact, which its own validator refuses by
    #: refusing every payload key.
    composed_by: str | None = None


@dataclass(frozen=True, slots=True)
class GovernedDependency:
    """One governed node a published Rule Set version depends on.

    ``object_kind`` names the Master Data registry the node lives in ('country'),
    so a family can depend on a node without this module knowing what the node
    means. ``hierarchy_version_id`` is the version the dependency was chosen
    under: without it, republishing the hierarchy changes what the rule matched
    with no version anywhere to diff.
    """

    object_kind: str
    node_id: str
    hierarchy_version_id: str | None = None
    label: str | None = None


_PROFILES: dict[str, RuleSetProfile] = {}


def register_profile(profile: RuleSetProfile) -> None:
    if not _KEY_RE.fullmatch(profile.key):
        raise RuleSetError("a profile key must be lowercase snake_case, 2 to 64 characters")
    if not _FAMILY_RE.fullmatch(profile.family):
        raise RuleSetError("a profile family must be lowercase snake_case, 2 to 40 characters")
    keys = [field.key for field in profile.form]
    if len(set(keys)) != len(keys):
        raise RuleSetError(f"profile {profile.key!r} declares the same form field twice")
    if profile.form and profile.composed_by:
        # Two doors onto one content is the defect `composed_by` exists to avoid.
        raise RuleSetError(
            f"profile {profile.key!r} declares a form AND names a gesture that "
            "composes it elsewhere; a version has one place it is written"
        )
    _PROFILES[profile.key] = profile


#: Every module that calls :func:`register_profile` at import time.
#:
#: Registration happens as a side effect of importing, so a caller that resolves
#: a profile without having imported its module sees an EMPTY registry and gets
#: "no Rule Set profile is registered" for a profile that exists. That is not
#: hypothetical: the Controls & Quality confirm route resolved profiles while
#: importing none of these, so `registered_profiles()` returned `()` behind a
#: mounted route and every Rule Set publication would have been refused.
#:
#: The list is explicit rather than a package scan so it is greppable, and
#: `test_every_profile_module_is_listed` fails if a new one is added without
#: joining it -- the alternative being that the omission surfaces as a 422 in
#: production for one family only.
_PROFILE_MODULES = (
    "core.controls_quality",
    "core.entity_rule_derivation",
    "core.money_policy",
    "core.source_currency_bindings",
    "core.tax_fee_rule_set",
)

_profiles_loaded = False


def load_profiles() -> None:
    """Import every profile module once. Idempotent, and safe to call anywhere.

    Deliberately lazy rather than a module-level import: these modules import
    FROM this one, so importing them at the top would be circular. Doing it on
    demand also means a future consumer inherits a populated registry without
    having to know this problem ever existed.
    """
    global _profiles_loaded
    if _profiles_loaded:
        return
    # Set BEFORE importing: a profile module that reaches back into this one
    # must not re-enter the loop.
    _profiles_loaded = True
    import importlib  # noqa: PLC0415

    for name in _PROFILE_MODULES:
        importlib.import_module(name)


def get_profile(key: str) -> RuleSetProfile:
    profile = _PROFILES.get(key)
    if profile is None:
        # A miss is the signal that the registry may simply not be populated yet.
        load_profiles()
        profile = _PROFILES.get(key)
    if profile is None:
        raise RuleSetError(f"no Rule Set profile is registered for {key!r}")
    return profile


def registered_profiles() -> tuple[RuleSetProfile, ...]:
    load_profiles()
    return tuple(sorted(_PROFILES.values(), key=lambda item: item.key))


# ---------------------------------------------------------------------------
# Pure helpers.
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _mint(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _required(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuleSetError(f"{label} is required")
    return value.strip()


def _family(value: Any) -> str:
    text = _required(value, "family")
    if not _FAMILY_RE.fullmatch(text):
        raise RuleSetError("family must be lowercase snake_case, 2 to 40 characters")
    return text


def _name(value: Any) -> str:
    text = _required(value, "name")
    if not _NAME_RE.fullmatch(text):
        raise RuleSetError("name must be lowercase snake_case, 1 to 127 characters")
    return text


def _as_date(value: Any, label: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise RuleSetError(f"{label} must be an ISO date (YYYY-MM-DD)") from exc


def _json(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


_RULE_SET_COLUMNS = (
    "id",
    "org_id",
    "project_id",
    "family",
    "name",
    "label",
    "scope",
    "lifecycle_status",
    "current_version_id",
    "pending_version_id",
    "last_known_good_version_id",
    "created_by",
    "created_at",
    "updated_at",
)

_VERSION_COLUMNS = (
    "id",
    "rule_set_id",
    "project_id",
    "version_number",
    "status",
    "family",
    "profile",
    "label",
    "description",
    "payload",
    "ordered_rules",
    "requires",
    "effective_from",
    "effective_to",
    "content_hash",
    "created_by",
    "created_at",
)


def _rule_set_dict(row: Sequence[Any]) -> dict[str, Any]:
    record = dict(zip(_RULE_SET_COLUMNS, row))
    record["scope"] = _json(record["scope"], {})
    return record


def _version_dict(row: Sequence[Any]) -> dict[str, Any]:
    record = dict(zip(_VERSION_COLUMNS, row))
    record["payload"] = _json(record["payload"], {})
    record["ordered_rules"] = _json(record["ordered_rules"], [])
    record["requires"] = _json(record["requires"], [])
    return record


# ---------------------------------------------------------------------------
# Reads. Every one is Project-scoped in SQL, never in a caller's filter.
# ---------------------------------------------------------------------------


def fetch_rule_set(
    conn,
    *,
    project_id: str,
    rule_set_id: str | None = None,
    family: str | None = None,
    name: str | None = None,
) -> dict[str, Any] | None:
    """One rule set head by id, or by (family, name) within the Project."""

    columns = ", ".join(_RULE_SET_COLUMNS)
    with conn.cursor() as cur:
        if rule_set_id:
            cur.execute(
                f"SELECT {columns} FROM app.governance_rule_sets "
                "WHERE id = %s AND project_id = %s",
                (rule_set_id, project_id),
            )
        elif family and name:
            cur.execute(
                f"SELECT {columns} FROM app.governance_rule_sets "
                "WHERE project_id = %s AND family = %s AND name = %s "
                "AND lifecycle_status <> 'archived'",
                (project_id, _family(family), _name(name)),
            )
        else:
            raise RuleSetError("fetch_rule_set needs a rule_set_id or a (family, name) pair")
        row = cur.fetchone()
    return _rule_set_dict(row) if row else None


def list_rule_sets(conn, *, project_id: str, family: str | None = None) -> list[dict[str, Any]]:
    columns = ", ".join(_RULE_SET_COLUMNS)
    with conn.cursor() as cur:
        if family:
            cur.execute(
                f"SELECT {columns} FROM app.governance_rule_sets "
                "WHERE project_id = %s AND family = %s ORDER BY family, name",
                (project_id, _family(family)),
            )
        else:
            cur.execute(
                f"SELECT {columns} FROM app.governance_rule_sets "
                "WHERE project_id = %s ORDER BY family, name",
                (project_id,),
            )
        return [_rule_set_dict(row) for row in cur.fetchall()]


def fetch_version(conn, *, project_id: str, version_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_VERSION_COLUMNS)} FROM app.governance_rule_set_versions "
            "WHERE id = %s AND project_id = %s",
            (version_id, project_id),
        )
        row = cur.fetchone()
    return _version_dict(row) if row else None


def require_version(conn, *, project_id: str, version_id: str) -> dict[str, Any]:
    """The version, or a refusal. Never a silent ``None`` a caller treats as empty."""

    version = fetch_version(conn, project_id=project_id, version_id=version_id)
    if version is None:
        raise RuleSetNotFound(f"rule set version {version_id} is not in this Project")
    return version


def active_version(
    conn, *, project_id: str, family: str, name: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The published head and its current version, or ``None`` when unpublished.

    Returns ``None`` only when the head is genuinely absent or has no current
    version. An UNREADABLE head raises through, so an outage can never be read
    as "this Project has no money policy" and fall back to a default.
    """

    head = fetch_rule_set(conn, project_id=project_id, family=family, name=name)
    if head is None or not head.get("current_version_id"):
        return None
    version = require_version(
        conn, project_id=project_id, version_id=str(head["current_version_id"])
    )
    return head, version


def list_versions(conn, *, project_id: str, rule_set_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_VERSION_COLUMNS)} FROM app.governance_rule_set_versions "
            "WHERE rule_set_id = %s AND project_id = %s ORDER BY version_number DESC",
            (rule_set_id, project_id),
        )
        return [_version_dict(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Mutations.
# ---------------------------------------------------------------------------


def ensure_rule_set(
    conn,
    *,
    org_id: str,
    project_id: str,
    family: str,
    name: str,
    label: str,
    actor: str,
    scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the (Project, family, name) head, creating a draft one if absent."""

    existing = fetch_rule_set(conn, project_id=project_id, family=family, name=name)
    if existing is not None:
        return existing
    rule_set_id = _mint("grs")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.governance_rule_sets
                (id, org_id, project_id, family, name, label, scope, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                rule_set_id,
                _required(org_id, "org_id"),
                _required(project_id, "project_id"),
                _family(family),
                _name(name),
                _required(label, "label"),
                canonical_json(dict(scope or {})),
                _required(actor, "actor"),
            ),
        )
    result = fetch_rule_set(conn, project_id=project_id, rule_set_id=rule_set_id)
    if result is None:  # pragma: no cover -- only under a concurrent delete
        raise RuleSetUnavailable("the rule set disappeared immediately after insert")
    return result


def draft_version(
    conn,
    *,
    project_id: str,
    rule_set_id: str,
    profile: str,
    label: str,
    payload: Mapping[str, Any],
    actor: str,
    description: str | None = None,
    ordered_rules: Sequence[Mapping[str, Any]] | None = None,
    requires: Sequence[Mapping[str, Any]] | None = None,
    effective_from: date | str | None = None,
    effective_to: date | str | None = None,
) -> dict[str, Any]:
    """Create the next draft version, with its payload normalized by the profile.

    Idempotent by content: an identical draft on the same head returns the row
    already stored rather than minting a rival version of identical content --
    the same property :func:`core.master_data.import_vocabulary_version` relies on.
    """

    head = fetch_rule_set(conn, project_id=project_id, rule_set_id=rule_set_id)
    if head is None:
        raise RuleSetNotFound(f"rule set {rule_set_id} is not in this Project")
    spec = get_profile(profile)
    if spec.family != head["family"]:
        raise RuleSetConflict(
            f"profile {profile!r} belongs to family {spec.family!r}, "
            f"not to this rule set's {head['family']!r}"
        )
    normalized = spec.validate(payload)
    proposed_rules = [dict(rule) for rule in (ordered_rules or [])]
    if spec.validate_rules is not None:
        rules = spec.validate_rules(proposed_rules)
    elif proposed_rules:
        # A family with no rule normalizer cannot be handed a ladder: storing it
        # would freeze content nothing has ever checked into an immutable version.
        raise RuleSetError(
            f"profile {spec.key!r} declares no ordered-rule schema, so it cannot "
            "carry ordered rules"
        )
    else:
        rules = []
    dependencies = _normalized_requires(requires, spec)
    starts = _as_date(effective_from, "effective_from")
    ends = _as_date(effective_to, "effective_to")
    if starts and ends and ends < starts:
        raise RuleSetError("effective_to cannot precede effective_from")

    digest = content_hash(
        {
            "rule_set_id": rule_set_id,
            "profile": spec.key,
            "payload": normalized,
            "ordered_rules": rules,
            "requires": dependencies,
            "effective_from": starts.isoformat() if starts else None,
            "effective_to": ends.isoformat() if ends else None,
        }
    )

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_VERSION_COLUMNS)} FROM app.governance_rule_set_versions "
            "WHERE rule_set_id = %s AND content_hash = %s",
            (rule_set_id, digest),
        )
        row = cur.fetchone()
        if row is not None:
            return _version_dict(row)
        cur.execute(
            """
            SELECT COALESCE(MAX(version_number), 0) + 1
            FROM app.governance_rule_set_versions WHERE rule_set_id = %s
            """,
            (rule_set_id,),
        )
        version_number = int(cur.fetchone()[0])
        version_id = _mint("grsv")
        cur.execute(
            """
            INSERT INTO app.governance_rule_set_versions
                (id, rule_set_id, project_id, version_number, status, family, profile,
                 label, description, payload, ordered_rules, requires,
                 effective_from, effective_to, content_hash, created_by)
            VALUES (%s, %s, %s, %s, 'draft', %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s, %s, %s, %s)
            """,
            (
                version_id,
                rule_set_id,
                project_id,
                version_number,
                head["family"],
                spec.key,
                _required(label, "label"),
                description,
                canonical_json(normalized),
                canonical_json(rules),
                canonical_json(dependencies),
                starts,
                ends,
                digest,
                _required(actor, "actor"),
            ),
        )
        cur.execute(
            "UPDATE app.governance_rule_sets SET pending_version_id = %s, "
            "lifecycle_status = CASE WHEN lifecycle_status = 'draft' THEN 'draft' "
            "ELSE lifecycle_status END, updated_at = NOW() "
            "WHERE id = %s AND project_id = %s",
            (version_id, rule_set_id, project_id),
        )
    return require_version(conn, project_id=project_id, version_id=version_id)


def _normalized_requires(
    requires: Sequence[Mapping[str, Any]] | None, spec: RuleSetProfile
) -> list[dict[str, Any]]:
    """Every dependency names an EXACT version. A bare object id is refused."""

    entries: list[dict[str, Any]] = []
    for item in requires or []:
        kind = _required(item.get("kind"), "requires[].kind")
        object_id = _required(item.get("object_id"), "requires[].object_id")
        version_id = item.get("version_id")
        if not isinstance(version_id, str) or not version_id.strip():
            raise RuleSetError(
                f"requires[] entry {kind}:{object_id} has no version_id; "
                "a dependency on 'the latest version' is not a dependency"
            )
        entries.append(
            {"kind": kind, "object_id": object_id, "version_id": version_id.strip()}
        )
    if spec.required_reference_kinds:
        present = {entry["kind"] for entry in entries}
        missing = [kind for kind in spec.required_reference_kinds if kind not in present]
        if missing:
            raise RuleSetError(
                f"profile {spec.key!r} requires a pinned reference of kind(s): "
                + ", ".join(missing)
            )
    return sorted(entries, key=lambda entry: (entry["kind"], entry["object_id"]))


def publish_version(
    conn, *, project_id: str, rule_set_id: str, version_id: str, actor: str
) -> dict[str, Any]:
    """Freeze a draft, make it current, and record the previous one as last-known-good.

    Ordering matters and is stated: ``last_known_good`` is set from the version
    that was current BEFORE this call, so a later failure rolls back to something
    that actually worked rather than to the version that just replaced it.
    """

    head = fetch_rule_set(conn, project_id=project_id, rule_set_id=rule_set_id)
    if head is None:
        raise RuleSetNotFound(f"rule set {rule_set_id} is not in this Project")
    version = require_version(conn, project_id=project_id, version_id=version_id)
    if version["rule_set_id"] != rule_set_id:
        raise RuleSetConflict("that version belongs to another rule set")
    if version["status"] == PUBLISHED:
        return version
    if version["status"] not in {"draft", "candidate"}:
        raise RuleSetConflict(f"a {version['status']} version cannot be published")

    previous_current = head.get("current_version_id")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.governance_rule_set_versions SET status = 'published' "
            "WHERE id = %s AND project_id = %s",
            (version_id, project_id),
        )
        if previous_current and previous_current != version_id:
            cur.execute(
                "UPDATE app.governance_rule_set_versions SET status = 'superseded' "
                "WHERE id = %s AND project_id = %s AND status = 'published'",
                (previous_current, project_id),
            )
        cur.execute(
            """
            UPDATE app.governance_rule_sets
            SET current_version_id = %s,
                last_known_good_version_id = COALESCE(%s, %s),
                pending_version_id = NULL,
                lifecycle_status = 'published',
                updated_at = NOW()
            WHERE id = %s AND project_id = %s
            """,
            (version_id, previous_current, version_id, rule_set_id, project_id),
        )
    _declare_governed_dependencies(
        conn,
        project_id=project_id,
        rule_set_id=rule_set_id,
        version=version,
        superseded_version_id=(
            previous_current if previous_current and previous_current != version_id else None
        ),
        actor=actor,
    )
    return require_version(conn, project_id=project_id, version_id=version_id)


def _declare_governed_dependencies(
    conn,
    *,
    project_id: str,
    rule_set_id: str,
    version: Mapping[str, Any],
    superseded_version_id: str | None,
    actor: str,
) -> None:
    """Write what this published version depends on, and release what the last one did.

    ON THE CALLER'S TRANSACTION, inside the same confirmation that made the version
    current. A dependency recorded in a second transaction can be lost while the
    version it describes is live, and the used-by guard would then authorize a
    market change that re-means a published figure -- the exact failure the guard
    exists to prevent.

    ORDER MATTERS. The new version registers FIRST and the superseded one is
    released after, both scoped by ``consumer_version_id``. The two versions
    routinely name the SAME market, so releasing by consumer alone -- or releasing
    first and registering after -- would leave the dependency that was just
    declared marked released.

    A family that declares nothing is the normal case (Money, FX, Timezone,
    Controls): no hook, no read, no write.
    """

    profile_key = str(version.get("profile") or "")
    if not profile_key:
        return
    try:
        profile = get_profile(profile_key)
    except RuleSetError:
        # A version whose profile module is not imported must not lose its
        # publication over a dependency declaration. It is logged, not swallowed
        # silently: an unwritten dependency is a guard running blind.
        logger.warning(
            "governance_rule_sets: profile %s unavailable, dependencies not declared "
            "for version %s",
            profile_key,
            version.get("id"),
        )
        return
    if profile.governed_dependencies is None:
        return

    dependencies = tuple(
        profile.governed_dependencies(
            version.get("payload") or {}, version.get("ordered_rules") or []
        )
    )

    from core.master_data import (  # noqa: PLC0415
        UsedByReference,
        register_used_by,
        release_used_by,
        require_registry,
    )

    registries: dict[str, str] = {}
    for dependency in dependencies:
        registry_id = registries.get(dependency.object_kind)
        if registry_id is None:
            registry = require_registry(
                conn, project_id=project_id, object_kind=dependency.object_kind
            )
            registry_id = str(registry["id"])
            registries[dependency.object_kind] = registry_id
        register_used_by(
            conn,
            project_id=project_id,
            registry_id=registry_id,
            reference=UsedByReference(
                node_id=dependency.node_id,
                consumer_kind=profile.key,
                consumer_id=rule_set_id,
                consumer_label=dependency.label,
                consumer_version_id=str(version["id"]),
                hierarchy_version_id=dependency.hierarchy_version_id,
            ),
            actor=actor,
        )

    if superseded_version_id:
        release_used_by(
            conn,
            project_id=project_id,
            consumer_kind=profile.key,
            consumer_id=rule_set_id,
            consumer_version_id=superseded_version_id,
        )


def record_exception(
    conn,
    *,
    project_id: str,
    rule_set_id: str,
    rule_set_version_id: str,
    subject_kind: str,
    subject_id: str,
    reason_code: str,
    reason: str,
    effective_from: date | str,
    decided_by: str,
    expires_at: date | str | None = None,
) -> str:
    """Record a version-bound, interval-bound exemption and return its id."""

    starts = _as_date(effective_from, "effective_from")
    if starts is None:
        raise RuleSetError("an exception needs an effective_from date")
    ends = _as_date(expires_at, "expires_at")
    if ends and ends < starts:
        raise RuleSetError("expires_at cannot precede effective_from")
    exception_id = _mint("grse")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.governance_rule_set_exceptions
                (id, project_id, rule_set_id, rule_set_version_id, subject_kind, subject_id,
                 reason_code, reason, effective_from, expires_at, decided_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                exception_id,
                project_id,
                rule_set_id,
                rule_set_version_id,
                _required(subject_kind, "subject_kind"),
                _required(subject_id, "subject_id"),
                _required(reason_code, "reason_code"),
                _required(reason, "reason"),
                starts,
                ends,
                _required(decided_by, "decided_by"),
            ),
        )
    return exception_id


def list_exceptions(
    conn, *, project_id: str, rule_set_id: str | None = None, on_date: date | None = None
) -> list[dict[str, Any]]:
    """Exceptions, optionally narrowed to those in force on a given date."""

    clauses = ["project_id = %s"]
    params: list[Any] = [project_id]
    if rule_set_id:
        clauses.append("rule_set_id = %s")
        params.append(rule_set_id)
    if on_date is not None:
        clauses.append("effective_from <= %s AND (expires_at IS NULL OR expires_at >= %s)")
        params.extend([on_date, on_date])
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, rule_set_id, rule_set_version_id, subject_kind, subject_id, "
            "reason_code, reason, effective_from, expires_at, decided_by, created_at "
            "FROM app.governance_rule_set_exceptions "
            f"WHERE {' AND '.join(clauses)} ORDER BY created_at DESC",
            params,
        )
        columns = (
            "id",
            "rule_set_id",
            "rule_set_version_id",
            "subject_kind",
            "subject_id",
            "reason_code",
            "reason",
            "effective_from",
            "expires_at",
            "decided_by",
            "created_at",
        )
        return [dict(zip(columns, row)) for row in cur.fetchall()]
