"""Tracked entities: identity, roles, source identities and candidate decisions (Story 48.5).

This is the GOVERNANCE half of Competitors. It answers two questions and
deliberately refuses a third:

* *Which entity is this?* -- one organization identity, versioned, aliased with
  typed claims, reused by every Project rather than retyped in each one.
* *What does a source call it?* -- a governed external identifier and the
  evidence for believing it. Meaning only: no report, no filter, no account
  configuration. Those are physical, they differ per Datastream, and they belong
  to :mod:`core.entity_bindings`.
* *May I compare their numbers?* -- not this module's question, and not this
  module's answer. Identity alignment resolves who is observed; commensurability
  is a separate Governance decision, and conflating them is how a matched search
  term silently authorizes a benchmark.

WHAT REPLACES WHAT

``core.tracked_entity_registry`` kept a mutable entity row and a JSONB array of
alias strings. Both are gone: identity lives on the generic Master Data objects
(:mod:`core.master_data`, organization scope) where a version is immutable and an
alias states WHICH claim it makes -- exact, close, broader, narrower, related or
negative. A list of strings cannot make that distinction, which is exactly how a
"related" alias becomes "the same thing" without anyone deciding.

``core.tracked_entity_matching`` returned ``outcome='resolved'``: a machine
writing an active match. The ranking here is kept -- exact, then normalized, then
similarity, with the same deterministic tie-break -- and its OUTPUT changed. It
produces a *proposal*. Only :func:`confirm_decision` writes a decision that
counts, and the database CHECK on ``entity_match_decisions`` refuses a confirmed
row that names no human.

One semantic correction is deliberate and worth stating: a similarity win is
recorded as ``close``, never ``exact``. SKOS is explicit that a close match is
not transitive, and the old code promoting a 0.9 difflib ratio to an equality is
the mechanism by which two different companies become one row.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from core.dimension_conformance import normalize_value, similarity
from core.master_data import (
    ALIAS_RELATIONS,
    VERSION_SCOPE_NODE,
    MasterDataConflict,
    MasterDataError,
    MasterDataNotFound,
    content_hash,
    create_node_version,
    create_org_node,
    create_org_registry,
    current_node_versions,
    ensure_type_version,
    fetch_org_node,
    fetch_org_registry,
    list_aliases,
    list_org_nodes,
    list_project_associations,
    normalize_alias_value,
    publish_node_version,
    record_alias,
    retire_alias,
    retire_project_association,
    set_project_association,
)

OBJECT_KIND = "tracked_entity"
REGISTRY_LABEL = "Tracked Entities"

#: The Project roles Competitors defines. The generic association table does not
#: know them -- it stores an opaque lowercase token -- which is what lets another
#: capability mount its own roles on the same machinery.
ROLE_OWN = "own"
ROLE_COMPETITOR = "competitor"
ROLE_REFERENCE = "reference"
PROJECT_ROLES = (ROLE_OWN, ROLE_COMPETITOR, ROLE_REFERENCE)

#: Entity kinds the platform ships a type for. This is a STARTING SET, not a
#: closed enum: the node_kind column takes any lowercase token and a client type
#: version can add one without a migration. Hard-coding "brand" as the only
#: shape is the mistake the ratified contract names explicitly.
KIND_BRAND = "brand"
KIND_ORGANIZATION = "organization"
KIND_DOMAIN = "domain"
KIND_PRODUCT = "product"
KIND_SOURCE_ENTITY = "source_entity"
DEFAULT_ENTITY_KINDS = (
    KIND_BRAND,
    KIND_ORGANIZATION,
    KIND_DOMAIN,
    KIND_PRODUCT,
    KIND_SOURCE_ENTITY,
)

#: SKOS relations a candidate decision may carry, plus `none` for "this value
#: denotes no governed identity" -- which is a decision, not an absence.
RELATION_EXACT = "exact"
RELATION_CLOSE = "close"
RELATION_BROADER = "broader"
RELATION_NARROWER = "narrower"
RELATION_RELATED = "related"
RELATION_NEGATIVE = "negative"
RELATION_NONE = "none"

STATE_PROPOSED = "proposed"
STATE_CONFIRMED = "confirmed"
STATE_REFUSED = "refused"
STATE_SUPERSEDED = "superseded"

REASON_RANKED = "ranked"
REASON_NO_CANDIDATE = "no_candidate"
REASON_BELOW_THRESHOLD = "below_threshold"
REASON_AMBIGUOUS = "ambiguous"
REASON_NEGATIVE_ALIAS = "negative_alias"
REASON_OPERATOR_CHOICE = "operator_choice"
REASON_PRIVACY_TRUNCATED = "privacy_truncated"

#: Which ranking implementation produced a score. Bumped by code, never by an
#: operator: a decision pins it so two decisions taken under different rankings
#: are never silently compared.
ALGORITHM_VERSION = "rank/1"

DEFAULT_THRESHOLDS = {RELATION_CLOSE: 0.88}
DEFAULT_AMBIGUITY_BAND = 0.03

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class TrackedEntityError(MasterDataError):
    """A governed tracked-entity operation was rejected."""

    code = "invalid_tracked_entity_operation"


def _mint(prefix: str) -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(26))
    return f"{prefix}_{body}"


def _clean(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise TrackedEntityError(f"{label} is required")
    return text


# ---------------------------------------------------------------------------
# The organization registry and its object types.
# ---------------------------------------------------------------------------

#: What a tracked entity IS, as a schema rather than as columns. Adding a
#: property is a new immutable type version, not a migration, which is the whole
#: reason 143 made object types a first-class thing.
ENTITY_PROPERTY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "required": ["preferred_label", "lifecycle_state"],
    "properties": {
        "preferred_label": {"type": "string", "minLength": 1, "maxLength": 200},
        "lifecycle_state": {
            "type": "string",
            "enum": ["draft", "active", "archived", "merged"],
        },
        "description": {"type": ["string", "null"]},
        "homepage": {"type": ["string", "null"]},
        "notes": {"type": ["string", "null"]},
    },
}

#: Governed relationships between identities. `navigation_only` is the important
#: word: a parent/child link here makes NO analytical claim, so nothing may
#: aggregate a subsidiary into a parent because someone drew an arrow.
ENTITY_RELATIONSHIPS: list[dict[str, Any]] = [
    {
        "key": "parent_organization",
        "label": "Parent organization",
        "target_kind": KIND_ORGANIZATION,
        "direction": "outbound",
        "cardinality": "many_to_one",
        "hierarchy": "dag",
        "aggregation": "navigation_only",
        "effective_dated": True,
    },
    {
        "key": "same_as",
        "label": "Same as",
        "target_kind": None,
        "direction": "symmetric",
        "cardinality": "many_to_many",
        "hierarchy": "flat",
        "aggregation": "navigation_only",
        "effective_dated": False,
    },
]


def ensure_registry(conn, *, org_id: str, actor: str) -> dict[str, Any]:
    """The organization's single tracked-entity owner, created on first use.

    ``version_scope='node'``: every identity has its own history. Under registry
    scope one publication would move a single pointer for the whole set, and the
    second entity's first version would collide with the first entity's.
    """

    registry = fetch_org_registry(conn, org_id=org_id, object_kind=OBJECT_KIND)
    if registry is not None:
        return registry
    return create_org_registry(
        conn,
        org_id=org_id,
        object_kind=OBJECT_KIND,
        label=REGISTRY_LABEL,
        actor=actor,
        version_scope=VERSION_SCOPE_NODE,
    )


def ensure_entity_type(conn, *, org_id: str, actor: str) -> dict[str, Any]:
    """Content-addressed: the same definition never mints a rival type version."""

    return ensure_type_version(
        conn,
        org_id=org_id,
        object_kind=OBJECT_KIND,
        label="Tracked entity",
        description=(
            "An identity a Project may track as its own, as a competitor or as a "
            "reference. Roles are per Project; the identity is not."
        ),
        property_schema=ENTITY_PROPERTY_SCHEMA,
        relationship_definitions=ENTITY_RELATIONSHIPS,
        origin="platform_template",
        actor=actor,
    )


# ---------------------------------------------------------------------------
# Identities.
# ---------------------------------------------------------------------------


def create_entity(
    conn,
    *,
    org_id: str,
    entity_kind: str,
    preferred_label: str,
    actor: str,
    aliases: Sequence[Mapping[str, Any]] = (),
    description: str | None = None,
    publish: bool = True,
) -> dict[str, Any]:
    """Mint one organization identity and its first immutable version.

    ``aliases`` entries are ``{value, relation, namespace?, locale?}``. They are
    recorded as typed claims, so a negative alias -- "this string is NOT us" --
    is expressible from the first write instead of being a later special case.
    """

    registry = ensure_registry(conn, org_id=org_id, actor=actor)
    type_version = ensure_entity_type(conn, org_id=org_id, actor=actor)
    label = _clean(preferred_label, "preferred_label")
    node = create_org_node(
        conn,
        org_id=org_id,
        registry_id=registry["id"],
        node_kind=_clean(entity_kind, "entity_kind"),
        label=label,
        actor=actor,
    )
    version = create_node_version(
        conn,
        org_id=org_id,
        registry_id=registry["id"],
        node_id=node["id"],
        type_version_id=type_version["id"],
        payload={
            "preferred_label": label,
            "lifecycle_state": "active",
            "description": description,
        },
        actor=actor,
    )
    if publish:
        version = publish_node_version(
            conn, org_id=org_id, version_id=version["id"], actor=actor
        )
    recorded = []
    for alias in aliases:
        recorded.append(
            record_alias(
                conn,
                org_id=org_id,
                node_id=node["id"],
                namespace=str(alias.get("namespace") or "operator"),
                raw_value=str(alias["value"]),
                relation=str(alias.get("relation") or RELATION_EXACT),
                locale=alias.get("locale"),
                provenance=str(alias.get("provenance") or "operator"),
                provenance_reference=alias.get("provenance_reference"),
                evidence=alias.get("evidence") or {},
                actor=actor,
            )
        )
    return {"entity": node, "version": version, "aliases": recorded}


def revise_entity(
    conn,
    *,
    org_id: str,
    node_id: str,
    actor: str,
    preferred_label: str | None = None,
    lifecycle_state: str | None = None,
    description: str | None = None,
    publish: bool = True,
) -> dict[str, Any]:
    """Record a NEW version. The previous one stays readable and stays pinned."""

    node = fetch_org_node(conn, org_id=org_id, node_id=node_id)
    if node is None:
        raise MasterDataNotFound("entity not found in this organization")
    current = current_node_versions(
        conn, org_id=org_id, registry_id=node["registry_id"], node_ids=[node_id]
    ).get(node_id)
    payload = dict((current or {}).get("payload") or {})
    if preferred_label is not None:
        payload["preferred_label"] = _clean(preferred_label, "preferred_label")
    if lifecycle_state is not None:
        payload["lifecycle_state"] = lifecycle_state
    if description is not None:
        payload["description"] = description
    payload.setdefault("lifecycle_state", "active")
    payload.setdefault("preferred_label", node["label"])

    version = create_node_version(
        conn,
        org_id=org_id,
        registry_id=node["registry_id"],
        node_id=node_id,
        type_version_id=(current or {}).get("type_version_id"),
        payload=payload,
        actor=actor,
    )
    if publish:
        version = publish_node_version(
            conn, org_id=org_id, version_id=version["id"], actor=actor
        )
    return version


def list_entities(conn, *, org_id: str) -> list[dict[str, Any]]:
    """The organization master: identity, current version and typed aliases."""

    registry = fetch_org_registry(conn, org_id=org_id, object_kind=OBJECT_KIND)
    if registry is None:
        return []
    nodes = list_org_nodes(conn, org_id=org_id, registry_id=registry["id"], include_archived=True)
    if not nodes:
        return []
    node_ids = [str(node["id"]) for node in nodes]
    versions = current_node_versions(
        conn, org_id=org_id, registry_id=registry["id"], node_ids=node_ids
    )
    aliases: dict[str, list[dict[str, Any]]] = {}
    for alias in list_aliases(conn, org_id=org_id, node_ids=node_ids):
        aliases.setdefault(str(alias["node_id"]), []).append(alias)
    return [
        {
            "entity_id": node["id"],
            "entity_kind": node["node_kind"],
            "label": node["label"],
            "archived_at": node["archived_at"],
            "current_version": versions.get(str(node["id"])),
            "aliases": aliases.get(str(node["id"]), []),
        }
        for node in nodes
    ]


def add_entity_alias(
    conn,
    *,
    org_id: str,
    node_id: str,
    value: str,
    relation: str,
    actor: str,
    namespace: str = "operator",
    locale: str | None = None,
    confidence: float | None = None,
    provenance: str = "operator",
    provenance_reference: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if relation not in ALIAS_RELATIONS:
        raise TrackedEntityError(f"unknown alias relation: {relation!r}")
    if fetch_org_node(conn, org_id=org_id, node_id=node_id) is None:
        raise MasterDataNotFound("entity not found in this organization")
    return record_alias(
        conn,
        org_id=org_id,
        node_id=node_id,
        namespace=namespace,
        raw_value=value,
        relation=relation,
        locale=locale,
        confidence=confidence,
        provenance=provenance,
        provenance_reference=provenance_reference,
        evidence=evidence,
        actor=actor,
    )


def remove_entity_alias(conn, *, org_id: str, alias_id: str, actor: str) -> dict[str, Any]:
    return retire_alias(conn, org_id=org_id, alias_id=alias_id, actor=actor)


# ---------------------------------------------------------------------------
# Project roles. An organization identity, assigned a meaning in ONE Project.
# ---------------------------------------------------------------------------


def set_project_role(
    conn,
    *,
    org_id: str,
    project_id: str,
    node_id: str,
    role: str,
    actor: str,
    node_version_id: str | None = None,
) -> dict[str, Any]:
    """Give an identity a role here. The same identity may hold another there."""

    if role not in PROJECT_ROLES:
        raise TrackedEntityError(f"unknown project role: {role!r}")
    node = fetch_org_node(conn, org_id=org_id, node_id=node_id)
    if node is None:
        raise MasterDataNotFound("entity not found in this organization")
    return set_project_association(
        conn,
        org_id=org_id,
        project_id=project_id,
        node_id=node_id,
        project_role=role,
        node_version_id=node_version_id,
        actor=actor,
    )


def retire_project_role(
    conn, *, project_id: str, association_id: str, actor: str
) -> dict[str, Any]:
    return retire_project_association(
        conn, project_id=project_id, association_id=association_id, actor=actor
    )


def project_roles(conn, *, project_id: str) -> list[dict[str, Any]]:
    """This Project's role assignments. There is no sibling-Project variant."""

    return list_project_associations(conn, project_id=project_id)


def project_registry(conn, *, org_id: str, project_id: str) -> list[dict[str, Any]]:
    """What a Project may see: its OWN roles, joined to the shared identities.

    An identity the Project has not associated is absent -- not listed with a
    null role, not counted. A Project cannot learn that a sibling tracks it,
    because the only rows this returns are the ones it created itself.
    """

    associations = list_project_associations(conn, project_id=project_id)
    if not associations:
        return []
    by_node = {str(item["node_id"]): item for item in associations}
    entities = [
        entity for entity in list_entities(conn, org_id=org_id)
        if str(entity["entity_id"]) in by_node
    ]
    for entity in entities:
        association = by_node[str(entity["entity_id"])]
        entity["role"] = association["project_role"]
        entity["association_id"] = association["id"]
        entity["pinned_version_id"] = association["node_version_id"]
    return entities


# ---------------------------------------------------------------------------
# Governed source identities. What a Connector calls an identity -- and nothing
# about which report, filter or account would fetch it.
# ---------------------------------------------------------------------------

_SOURCE_IDENTITY_COLUMNS = (
    "id",
    "org_id",
    "node_id",
    "node_version_id",
    "connector_name",
    "account_scope",
    "external_id",
    "external_label",
    "version_number",
    "provenance",
    "provenance_reference",
    "evidence",
    "content_hash",
    "created_by",
    "created_at",
    "retired_at",
    "retired_by",
)


def record_source_identity(
    conn,
    *,
    org_id: str,
    node_id: str,
    connector_name: str,
    external_id: str,
    actor: str,
    account_scope: str | None = None,
    external_label: str | None = None,
    node_version_id: str | None = None,
    provenance: str = "operator",
    provenance_reference: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record what one Connector calls this identity.

    Idempotent on content: recording the same identifier twice returns the
    existing live row rather than minting a rival version, so a replayed
    confirmation cannot produce two governed answers to the same question.
    """

    if fetch_org_node(conn, org_id=org_id, node_id=node_id) is None:
        raise MasterDataNotFound("entity not found in this organization")
    connector = _clean(connector_name, "connector_name")
    identifier = _clean(external_id, "external_id")
    digest = content_hash(
        {
            "node_id": node_id,
            "connector_name": connector,
            "account_scope": account_scope,
            "external_id": identifier,
        }
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_SOURCE_IDENTITY_COLUMNS)}
            FROM app.entity_source_identity_versions
            WHERE org_id = %s AND connector_name = %s
              AND COALESCE(account_scope, '') = COALESCE(%s, '')
              AND external_id = %s AND retired_at IS NULL
            """,
            (org_id, connector, account_scope, identifier),
        )
        row = cur.fetchone()
        if row is not None:
            existing = dict(zip(_SOURCE_IDENTITY_COLUMNS, row, strict=False))
            if str(existing["node_id"]) != str(node_id):
                raise MasterDataConflict(
                    "that source identifier already denotes a different identity; "
                    "retire the existing representation before recording this one"
                )
            return existing
        cur.execute(
            """
            SELECT COALESCE(MAX(version_number), 0) + 1
            FROM app.entity_source_identity_versions
            WHERE org_id = %s AND node_id = %s AND connector_name = %s
            """,
            (org_id, node_id, connector),
        )
        (next_number,) = cur.fetchone()
        cur.execute(
            f"""
            INSERT INTO app.entity_source_identity_versions
                (id, org_id, node_id, node_version_id, connector_name, account_scope,
                 external_id, external_label, version_number, provenance,
                 provenance_reference, evidence, content_hash, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            RETURNING {", ".join(_SOURCE_IDENTITY_COLUMNS)}
            """,
            (
                _mint("esi"),
                org_id,
                node_id,
                node_version_id,
                connector,
                account_scope,
                identifier,
                external_label,
                next_number,
                provenance,
                provenance_reference,
                json.dumps(dict(evidence or {})),
                digest,
                _clean(actor, "actor"),
            ),
        )
        result = cur.fetchone()
    if result is None:  # pragma: no cover - RETURNING always yields on success
        raise MasterDataConflict("source identity could not be recorded")
    return dict(zip(_SOURCE_IDENTITY_COLUMNS, result, strict=False))


def retire_source_identity(
    conn, *, org_id: str, source_identity_id: str, actor: str
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.entity_source_identity_versions
               SET retired_at = NOW(), retired_by = %s
             WHERE org_id = %s AND id = %s AND retired_at IS NULL
            RETURNING {", ".join(_SOURCE_IDENTITY_COLUMNS)}
            """,
            (_clean(actor, "actor"), org_id, source_identity_id),
        )
        row = cur.fetchone()
    if row is None:
        raise MasterDataNotFound("source identity not found in this organization")
    return dict(zip(_SOURCE_IDENTITY_COLUMNS, row, strict=False))


def list_source_identities(
    conn,
    *,
    org_id: str,
    node_ids: Sequence[str] | None = None,
    connector_name: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["org_id = %s", "retired_at IS NULL"]
    params: list[Any] = [org_id]
    if node_ids is not None:
        if not node_ids:
            return []
        clauses.append("node_id = ANY(%s)")
        params.append(list(node_ids))
    if connector_name:
        clauses.append("connector_name = %s")
        params.append(connector_name)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_SOURCE_IDENTITY_COLUMNS)}
            FROM app.entity_source_identity_versions
            WHERE {" AND ".join(clauses)}
            ORDER BY connector_name, node_id, external_id
            """,
            tuple(params),
        )
        return [
            dict(zip(_SOURCE_IDENTITY_COLUMNS, row, strict=False)) for row in cur.fetchall()
        ]


# ---------------------------------------------------------------------------
# Ranking. Pure, deterministic, and incapable of deciding anything.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """One ranked possibility for an observed value."""

    node_id: str
    matched_surface: str
    relation: str
    score: float
    method: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "matched_surface": self.matched_surface,
            "relation": self.relation,
            "score": round(self.score, 4),
            "method": self.method,
        }


@dataclass(frozen=True, slots=True)
class Proposal:
    """What ranking is allowed to produce: a suggestion and its reasons.

    There is no `resolved` here, and that absence is the design. The type cannot
    express "the machine decided", so no caller can accidentally persist it.
    """

    normalized_value: str
    relation: str
    reason_code: str
    candidates: tuple[Candidate, ...] = ()
    confidence: float | None = None
    features: Mapping[str, Any] = field(default_factory=dict)

    @property
    def top(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


@dataclass(frozen=True, slots=True)
class CorpusEntry:
    """One identity shaped for ranking, with its normalized surfaces precomputed."""

    node_id: str
    label: str
    #: (verbatim, normalized, relation) for every non-negative alias and the label.
    surfaces: tuple[tuple[str, str, str], ...]
    #: Normalized strings this identity has explicitly REFUSED. Checked first.
    refused: frozenset[str] = frozenset()


def build_corpus(entities: Sequence[Mapping[str, Any]]) -> list[CorpusEntry]:
    """Shape :func:`list_entities` output for ranking. Pure."""

    corpus: list[CorpusEntry] = []
    for entity in entities:
        node_id = entity.get("entity_id")
        if not node_id:
            continue
        version = entity.get("current_version") or {}
        payload = version.get("payload") or {}
        label = str(payload.get("preferred_label") or entity.get("label") or "")
        surfaces: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        refused: set[str] = set()
        if label:
            surfaces.append((label, normalize_value(label), RELATION_EXACT))
            seen.add(normalize_value(label))
        for alias in entity.get("aliases") or []:
            relation = str(alias.get("relation") or RELATION_EXACT)
            raw = str(alias.get("raw_value") or "")
            normalized = normalize_value(raw)
            if not normalized:
                continue
            if relation == RELATION_NEGATIVE:
                refused.add(normalized)
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            surfaces.append((raw, normalized, relation))
        corpus.append(
            CorpusEntry(
                node_id=str(node_id),
                label=label,
                surfaces=tuple(surfaces),
                refused=frozenset(refused),
            )
        )
    return corpus


def _sort_key(entry: CorpusEntry, score: float) -> tuple[Any, ...]:
    """Deterministic tie-break, so the same corpus always ranks the same way."""

    return (-score, entry.label, entry.node_id)


def rank_candidates(
    raw_value: str,
    corpus: Sequence[CorpusEntry] | Sequence[Mapping[str, Any]],
    *,
    thresholds: Mapping[str, float] | None = None,
    ambiguity_band: float = DEFAULT_AMBIGUITY_BAND,
) -> Proposal:
    """Rank the identities an observed value might denote. Pure, no I/O.

    Three stages, first one that clears wins: a verbatim hit, then a normalized
    hit, then similarity. The first two propose ``exact``; similarity proposes
    ``close`` and never ``exact``, because a difflib ratio is a resemblance and
    SKOS is explicit that a close match is not transitive.

    Every outcome is expressible: no candidate, below threshold, ambiguous, or a
    refusal recorded against the value. None of them is a decision.
    """

    entries: list[CorpusEntry] = []
    for item in corpus:
        entries.append(item if isinstance(item, CorpusEntry) else build_corpus([item])[0])

    thresholds = dict(DEFAULT_THRESHOLDS) | dict(thresholds or {})
    close_threshold = float(thresholds.get(RELATION_CLOSE, DEFAULT_THRESHOLDS[RELATION_CLOSE]))
    normalized = normalize_value(raw_value)

    if not normalized:
        return Proposal(
            normalized_value="",
            relation=RELATION_NONE,
            reason_code=REASON_NO_CANDIDATE,
            features={"empty_after_normalization": True},
        )

    refusing = [entry.node_id for entry in entries if normalized in entry.refused]
    pool = [entry for entry in entries if normalized not in entry.refused]
    if refusing and not pool:
        return Proposal(
            normalized_value=normalized,
            relation=RELATION_NONE,
            reason_code=REASON_NEGATIVE_ALIAS,
            features={"refused_by": sorted(refusing)},
        )
    if not pool:
        return Proposal(
            normalized_value=normalized,
            relation=RELATION_NONE,
            reason_code=REASON_NO_CANDIDATE,
        )

    features: dict[str, Any] = {"corpus_size": len(pool)}
    if refusing:
        features["refused_by"] = sorted(refusing)

    for method, predicate in (
        ("exact", lambda verbatim, norm: verbatim == raw_value),
        ("normalized", lambda verbatim, norm: norm == normalized),
    ):
        hits: list[Candidate] = []
        for entry in pool:
            for verbatim, norm, relation in entry.surfaces:
                if predicate(verbatim, norm):
                    hits.append(
                        Candidate(entry.node_id, verbatim, relation, 1.0, method)
                    )
                    break
        if not hits:
            continue
        if len({hit.node_id for hit in hits}) > 1:
            # The same string names two identities. That is a homonym, and the
            # server refusing to pick is the point.
            return Proposal(
                normalized_value=normalized,
                relation=RELATION_NONE,
                reason_code=REASON_AMBIGUOUS,
                candidates=tuple(sorted(hits, key=lambda item: item.node_id)),
                confidence=1.0,
                features=features | {"stage": method},
            )
        return Proposal(
            normalized_value=normalized,
            relation=hits[0].relation,
            reason_code=REASON_RANKED,
            candidates=tuple(hits),
            confidence=1.0,
            features=features | {"stage": method},
        )

    scored: list[tuple[CorpusEntry, Candidate]] = []
    for entry in pool:
        best: Candidate | None = None
        for verbatim, norm, _relation in entry.surfaces:
            if not norm:
                continue
            score = similarity(normalized, norm)
            if best is None or score > best.score:
                best = Candidate(entry.node_id, verbatim, RELATION_CLOSE, score, "similarity")
        if best is not None:
            scored.append((entry, best))
    scored.sort(key=lambda pair: _sort_key(pair[0], pair[1].score))
    ranked = tuple(candidate for _entry, candidate in scored)

    if not ranked:
        return Proposal(
            normalized_value=normalized,
            relation=RELATION_NONE,
            reason_code=REASON_NO_CANDIDATE,
            features=features,
        )
    top = ranked[0]
    features = features | {"stage": "similarity", "threshold": close_threshold}
    if top.score < close_threshold:
        return Proposal(
            normalized_value=normalized,
            relation=RELATION_NONE,
            reason_code=REASON_BELOW_THRESHOLD,
            candidates=ranked,
            confidence=top.score,
            features=features,
        )
    if len(ranked) >= 2 and top.score - ranked[1].score <= ambiguity_band:
        return Proposal(
            normalized_value=normalized,
            relation=RELATION_NONE,
            reason_code=REASON_AMBIGUOUS,
            candidates=ranked,
            confidence=top.score,
            features=features | {"ambiguity_band": ambiguity_band},
        )
    return Proposal(
        normalized_value=normalized,
        relation=RELATION_CLOSE,
        reason_code=REASON_RANKED,
        candidates=ranked,
        confidence=top.score,
        features=features,
    )


# ---------------------------------------------------------------------------
# The match policy a decision pins.
# ---------------------------------------------------------------------------

_POLICY_COLUMNS = (
    "id",
    "org_id",
    "version_number",
    "algorithm_version",
    "corpus_version",
    "thresholds",
    "ambiguity_band",
    "content_hash",
    "created_by",
    "created_at",
)


def corpus_version(corpus: Sequence[CorpusEntry]) -> str:
    """A hash over exactly what ranking saw. Two runs over different corpora are
    different facts, even when the scores match."""

    return content_hash(
        [
            {
                "node_id": entry.node_id,
                "surfaces": [list(surface) for surface in entry.surfaces],
                "refused": sorted(entry.refused),
            }
            for entry in sorted(corpus, key=lambda item: item.node_id)
        ]
    )


def ensure_policy_version(
    conn,
    *,
    org_id: str,
    corpus: Sequence[CorpusEntry],
    actor: str,
    thresholds: Mapping[str, float] | None = None,
    ambiguity_band: float = DEFAULT_AMBIGUITY_BAND,
) -> dict[str, Any]:
    """Content-address the policy. The same policy over the same corpus is one row."""

    resolved = dict(DEFAULT_THRESHOLDS) | dict(thresholds or {})
    digest_corpus = corpus_version(corpus)
    digest = content_hash(
        {
            "algorithm_version": ALGORITHM_VERSION,
            "corpus_version": digest_corpus,
            "thresholds": resolved,
            "ambiguity_band": ambiguity_band,
        }
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_POLICY_COLUMNS)}
            FROM app.entity_match_policy_versions
            WHERE org_id = %s AND content_hash = %s
            """,
            (org_id, digest),
        )
        row = cur.fetchone()
        if row is not None:
            return dict(zip(_POLICY_COLUMNS, row, strict=False))
        cur.execute(
            """
            SELECT COALESCE(MAX(version_number), 0) + 1
            FROM app.entity_match_policy_versions WHERE org_id = %s
            """,
            (org_id,),
        )
        (next_number,) = cur.fetchone()
        cur.execute(
            f"""
            INSERT INTO app.entity_match_policy_versions
                (id, org_id, version_number, algorithm_version, corpus_version,
                 thresholds, ambiguity_band, content_hash, created_by)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            RETURNING {", ".join(_POLICY_COLUMNS)}
            """,
            (
                _mint("emp"),
                org_id,
                next_number,
                ALGORITHM_VERSION,
                digest_corpus,
                json.dumps(resolved),
                ambiguity_band,
                digest,
                _clean(actor, "actor"),
            ),
        )
        return dict(zip(_POLICY_COLUMNS, cur.fetchone(), strict=False))


# ---------------------------------------------------------------------------
# Decisions. Ranking proposes; a person decides; nothing else writes here.
# ---------------------------------------------------------------------------

_DECISION_COLUMNS = (
    "id",
    "org_id",
    "project_id",
    "observation_id",
    "normalized_value",
    "raw_value_hash",
    "state",
    "relation",
    "node_id",
    "node_version_id",
    "ranked_candidates",
    "confidence",
    "reason_code",
    "reason_features",
    "policy_version_id",
    "supersedes_id",
    "proposed_by",
    "decided_by",
    "decided_at",
    "created_at",
)


def _decision(row: Any) -> dict[str, Any]:
    return dict(zip(_DECISION_COLUMNS, row, strict=False))


def record_proposal(
    conn,
    *,
    org_id: str,
    project_id: str | None,
    raw_value_hash: str,
    proposal: Proposal,
    policy_version_id: str,
    actor: str,
    observation_id: str | None = None,
) -> dict[str, Any]:
    """Persist a ranking outcome as a PROPOSAL.

    Idempotent on the observed value: a value that already has a live decision --
    proposed, confirmed or refused -- keeps it. Re-running the matcher must not
    resurrect a question a person already answered, which is the behaviour a
    refused mapping needs in order to stay refused.
    """

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_DECISION_COLUMNS)}
            FROM app.entity_match_decisions
            WHERE org_id = %s AND COALESCE(project_id, '') = COALESCE(%s, '')
              AND raw_value_hash = %s AND state <> 'superseded'
            """,
            (org_id, project_id, raw_value_hash),
        )
        row = cur.fetchone()
        if row is not None:
            return _decision(row)
        cur.execute(
            f"""
            INSERT INTO app.entity_match_decisions
                (id, org_id, project_id, observation_id, normalized_value, raw_value_hash,
                 state, relation, node_id, ranked_candidates, confidence, reason_code,
                 reason_features, policy_version_id, proposed_by)
            VALUES (%s, %s, %s, %s, %s, %s, 'proposed', %s, %s, %s::jsonb,
                    %s, %s, %s::jsonb, %s, %s)
            RETURNING {", ".join(_DECISION_COLUMNS)}
            """,
            (
                _mint("emd"),
                org_id,
                project_id,
                observation_id,
                proposal.normalized_value,
                raw_value_hash,
                # A proposal never claims a relation it has not evidenced; the
                # relation only becomes the proposed one once a human takes it.
                RELATION_NONE,
                None,
                json.dumps([item.as_dict() for item in proposal.candidates]),
                proposal.confidence,
                proposal.reason_code,
                json.dumps(
                    dict(proposal.features) | {"suggested_relation": proposal.relation}
                ),
                policy_version_id,
                _clean(actor, "actor"),
            ),
        )
        return _decision(cur.fetchone())


def confirm_decision(
    conn,
    *,
    org_id: str,
    decision_id: str,
    node_id: str,
    relation: str,
    actor: str,
    node_version_id: str | None = None,
) -> dict[str, Any]:
    """A person says what this value denotes.

    The observed value is read from the stored row, never from the caller. A
    correction that could supply its own raw string would let a client decide
    what it was correcting, and the audit would record the substitution as if it
    were the observation.
    """

    if relation not in (
        RELATION_EXACT,
        RELATION_CLOSE,
        RELATION_BROADER,
        RELATION_NARROWER,
        RELATION_RELATED,
    ):
        raise TrackedEntityError(f"a confirmation cannot carry relation {relation!r}")
    if fetch_org_node(conn, org_id=org_id, node_id=node_id) is None:
        raise MasterDataNotFound("entity not found in this organization")
    existing = _require_decision(conn, org_id=org_id, decision_id=decision_id)
    if existing["state"] != STATE_PROPOSED:
        raise MasterDataConflict("only a proposed decision can be confirmed")
    return _supersede_with(
        conn,
        previous=existing,
        state=STATE_CONFIRMED,
        relation=relation,
        node_id=node_id,
        node_version_id=node_version_id,
        reason_code=REASON_OPERATOR_CHOICE,
        actor=actor,
    )


def refuse_decision(
    conn, *, org_id: str, decision_id: str, actor: str, node_id: str | None = None
) -> dict[str, Any]:
    """A person says this value denotes nothing here -- or explicitly not THAT.

    ``node_id`` present records a `negative` relation: not merely unmatched, but
    refused against one identity, so the matcher stops offering it.
    """

    existing = _require_decision(conn, org_id=org_id, decision_id=decision_id)
    if existing["state"] != STATE_PROPOSED:
        raise MasterDataConflict("only a proposed decision can be refused")
    if node_id is not None and fetch_org_node(conn, org_id=org_id, node_id=node_id) is None:
        raise MasterDataNotFound("entity not found in this organization")
    return _supersede_with(
        conn,
        previous=existing,
        state=STATE_REFUSED,
        relation=RELATION_NEGATIVE if node_id else RELATION_NONE,
        node_id=node_id,
        node_version_id=None,
        reason_code=REASON_OPERATOR_CHOICE,
        actor=actor,
    )


def _require_decision(conn, *, org_id: str, decision_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_DECISION_COLUMNS)}
            FROM app.entity_match_decisions
            WHERE org_id = %s AND id = %s
            """,
            (org_id, decision_id),
        )
        row = cur.fetchone()
    if row is None:
        raise MasterDataNotFound("decision not found in this organization")
    return _decision(row)


def _supersede_with(
    conn,
    *,
    previous: Mapping[str, Any],
    state: str,
    relation: str,
    node_id: str | None,
    node_version_id: str | None,
    reason_code: str,
    actor: str,
) -> dict[str, Any]:
    """Write the new decision and retire the old one in ONE statement pair.

    Order matters: the live-decision unique index covers proposed, confirmed and
    refused, so the previous row must leave that set before the new one enters
    it. Both statements run on the caller's transaction -- a partial application
    is impossible because there is nothing to commit in between.
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.entity_match_decisions
               SET state = 'superseded'
             WHERE id = %s AND state = %s
            """,
            (previous["id"], previous["state"]),
        )
        if cur.rowcount != 1:
            raise MasterDataConflict(
                "the decision changed while it was being reviewed; re-read it and decide again"
            )
        cur.execute(
            f"""
            INSERT INTO app.entity_match_decisions
                (id, org_id, project_id, observation_id, normalized_value, raw_value_hash,
                 state, relation, node_id, node_version_id, ranked_candidates, confidence,
                 reason_code, reason_features, policy_version_id, supersedes_id,
                 proposed_by, decided_by, decided_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb,
                    %s, %s, %s, %s, NOW())
            RETURNING {", ".join(_DECISION_COLUMNS)}
            """,
            (
                _mint("emd"),
                previous["org_id"],
                previous["project_id"],
                previous["observation_id"],
                previous["normalized_value"],
                previous["raw_value_hash"],
                state,
                relation,
                node_id,
                node_version_id,
                json.dumps(previous["ranked_candidates"] or []),
                previous["confidence"],
                reason_code,
                json.dumps(dict(previous["reason_features"] or {})),
                previous["policy_version_id"],
                previous["id"],
                previous["proposed_by"],
                _clean(actor, "actor"),
            ),
        )
        return _decision(cur.fetchone())


def list_decisions(
    conn,
    *,
    org_id: str,
    project_id: str | None = None,
    states: Sequence[str] = (STATE_PROPOSED, STATE_CONFIRMED, STATE_REFUSED),
    limit: int = 200,
) -> list[dict[str, Any]]:
    clauses = ["org_id = %s", "state = ANY(%s)"]
    params: list[Any] = [org_id, list(states)]
    if project_id is not None:
        clauses.append("COALESCE(project_id, '') = %s")
        params.append(project_id)
    params.append(max(1, min(int(limit), 1000)))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_DECISION_COLUMNS)}
            FROM app.entity_match_decisions
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            tuple(params),
        )
        return [_decision(row) for row in cur.fetchall()]


def confirmed_identities(conn, *, org_id: str, project_id: str) -> dict[str, str]:
    """Normalized observed value -> the identity a person confirmed it denotes."""

    resolved: dict[str, str] = {}
    for decision in list_decisions(
        conn, org_id=org_id, project_id=project_id, states=(STATE_CONFIRMED,), limit=1000
    ):
        if decision["node_id"]:
            resolved[str(decision["normalized_value"])] = str(decision["node_id"])
    return resolved


__all__ = [
    "ALGORITHM_VERSION",
    "DEFAULT_ENTITY_KINDS",
    "OBJECT_KIND",
    "PROJECT_ROLES",
    "Candidate",
    "CorpusEntry",
    "Proposal",
    "TrackedEntityError",
    "add_entity_alias",
    "build_corpus",
    "confirm_decision",
    "confirmed_identities",
    "corpus_version",
    "create_entity",
    "ensure_policy_version",
    "ensure_registry",
    "list_decisions",
    "list_entities",
    "list_source_identities",
    "normalize_alias_value",
    "project_registry",
    "project_roles",
    "rank_candidates",
    "record_proposal",
    "record_source_identity",
    "refuse_decision",
    "remove_entity_alias",
    "retire_project_role",
    "retire_source_identity",
    "revise_entity",
    "set_project_role",
]
