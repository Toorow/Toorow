"""toorow -- the REFERENCE route of a managed-feed import (Story 68.5).

Epic 68 routes ONE import engine three ways, by declaration and never by
guessing from the rows:

  * facts     -- the default: typed rows to the project-scoped raw landing;
  * events    -- ``kind: event`` columns to ``context_events`` (Story 68.4
                 plugs into the same router);
  * reference -- a file whose pinned mapping declares entity REFERENCE columns
                 lands as versioned MDM node attributes, never as facts.

WHAT MAKES A MAPPING A REFERENCE MAPPING. Three declarations must hold at
once, all read from the pinned mapping/projection bundle:

  1. exactly ONE column carries 68.2's entity designation
     (``fields[].binding.designates_object_kind``) -- the column that names
     WHICH entity each row describes. Several designations is the
     facts-plus-matching shape (68.3/69), not a reference file;
  2. that column is part of the mapping's grain -- the file's identity IS the
     entity key (68.1's ``source_key`` reading: the grain is the identity);
  3. the pinned projection declares NO additive measures -- a file with
     measures carries facts, and facts take the warehouse route.

THE WRITES, AND THE ONLY WRITERS. ``master_data.create_node`` (first sight of
an entity key) and ``master_data.create_node_version`` (every landing) mint
the versions; ``master_data.publish_node_version`` makes each minted version
current. Nothing here ever issues a raw INSERT, and nothing ever updates a
prior version -- a changed snapshot APPENDS, so the version chain stays
queryable as-of.

THE HONEST NO-OP, AT TWO GRAINS. A byte-identical re-import never reaches
this module: the ledger's unchanged-snapshot oracle (OUTCOME_NOOP) fires
first, and it can only fire because a landed reference import marks its ledger
row ``published`` -- the MDM publish IS this route's publication act. A
CHANGED snapshot lands here, and the per-entity rule applies: an entity whose
attribute payload hashes to the same content digest as its current version
mints NOTHING (the content-addressed digest in ``create_node_version`` is the
identity; import provenance is deliberately outside it, migration 295).

AD-2: the entity kind is read from the pinned mapping and the registry as an
opaque string. No kind literal appears in this module, and none may.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping

from core.file_source_resolution import (
    _CONFIRMED_BINDING_STATES,
)
from core.master_data import (
    MasterDataError,
    content_hash,
    create_node,
    create_node_version,
    ensure_type_version,
    fetch_registry,
    publish_node_version,
)
from core.object_kind_registry import (
    entity_designations,
    fetch_entity_type_lookup,
    validate_entity_designations,
)
from core.tabular_parsing import (
    _truncate_value,
)
from core.tabular_types import (
    CsvExcelImportError,
    RejectedRow,
)

logger = logging.getLogger(__name__)


#: The route name the import runner's ONE router returns for this destination.
#: Deliberately NOT a ``file_source_template.LANDING_TARGETS`` member: a
#: template cannot declare it -- the route exists only when the pinned MAPPING
#: declares reference columns.
ROUTE_ENTITY_REFERENCE = "entity_reference"

#: The ledger's ``landing_relation`` prefix for a reference landing. The
#: relation is a free string the ledger never parses (the plan-store
#: precedent); the prefix is what ``assert_managed_landing`` whitelists, so a
#: caller can never point the writer at a mart.
LANDING_RELATION_PREFIX = "entity_reference:"

#: Rejection rule codes (closed). Per-row evidence in
#: ``app.managed_feed_rejected_rows`` carries one of these, the key field and
#: the file line -- never a silent drop (AC4).
RULE_KEY_MISSING = "entity_reference_key_missing"
RULE_KEY_TOO_LONG = "entity_reference_key_too_long"

#: A node label is the entity key; ``master_data`` refuses labels past this.
_LABEL_MAX = 120


# ---------------------------------------------------------------------------
# The route declaration. Pure, so the routing rule is provable without a db.
# ---------------------------------------------------------------------------


def reference_designation(
    mapping_payload: Mapping[str, Any] | None,
    projection_plan: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """The reference-route declaration of a pinned mapping, or None.

    Reads the three declarations named in the module docstring and returns
    ``{field_id, object_kind, key_target, attribute_targets}`` when all hold:

    * ``field_id`` / ``object_kind`` -- the designating column and the entity
      type it designates (68.2, read through the registry module's own pure
      reader so the two never disagree on the payload's shape);
    * ``key_target`` -- the canonical target the governed mapping renames that
      column to; the landed rows carry the key under THIS name;
    * ``attribute_targets`` -- the canonical targets of every other included
      column, in mapping order: the attributes each row carries.

    The included/confirmed classification mirrors ``_apply_governed_mapping``
    exactly (``_CONFIRMED_BINDING_STATES`` + a named canonical target): a
    column the mapping would not land cannot name the route either.
    """
    pairs = entity_designations(mapping_payload)
    if len(set(pairs)) != 1:
        # Zero designations: an ordinary mapping. Several: the
        # facts-plus-matching shape (68.3/69) -- NOT a reference file, and
        # never a silent pick between two keys.
        return None
    field_id, object_kind = pairs[0]

    grain = (mapping_payload or {}).get("grain") or []
    if field_id not in [str(field) for field in grain]:
        # The entity key is not the file's identity: the rows are keyed by
        # something else (a date, a placement), so they are facts about the
        # entity, not its reference attributes.
        return None

    if (projection_plan or {}).get("additive_measures"):
        # A file with measures carries facts; facts take the warehouse route.
        return None

    key_target = ""
    attribute_targets: list[str] = []
    attribute_types: dict[str, str] = {}
    for item in (mapping_payload or {}).get("fields") or []:
        if not isinstance(item, Mapping):
            continue
        source = str(item.get("field_id") or "").strip()
        binding = item.get("binding") or {}
        status = str(binding.get("status") or "").lower()
        if status == "excluded" or status not in _CONFIRMED_BINDING_STATES:
            continue
        target = str(binding.get("canonical_target") or "").strip()
        if not source or not target:
            continue
        if source == field_id:
            key_target = target
        else:
            attribute_targets.append(target)
            attribute_types[target] = str(item.get("physical_type") or "").strip().lower()
    if not key_target:
        # The designating column itself is excluded or unconfirmed: the
        # designation designates nothing the import would land.
        return None

    return {
        "field_id": field_id,
        "object_kind": object_kind,
        "key_target": key_target,
        "attribute_targets": attribute_targets,
        # Le TYPE que le mapping declare pour chaque attribut. Il sert a poser
        # le schema de proprietes du type d'entite (voir `property_schema`) :
        # deviner le type depuis la premiere valeur non nulle marcherait
        # jusqu'au fichier ou la premiere ligne est vide.
        "attribute_types": attribute_types,
    }


def resolve_reference_route(
    conn,
    *,
    project_id: str,
    mapping_payload: Mapping[str, Any] | None,
    projection_plan: Mapping[str, Any] | None,
    publish_candidate: bool,
) -> dict[str, Any] | None:
    """The reference route, re-verified against the LIVE registry, or None.

    68.2 validates a designation when the mapping version is appended and when
    it publishes. Neither covers the day after: an entity type ARCHIVED while
    a mapping pin already exists would route rows into a registry nobody may
    write anymore. The import re-verifies under the same pure validator, with
    the registry lookup the registry module owns -- fail closed, naming the
    repair, exactly like the append gate.
    """
    designation = reference_designation(mapping_payload, projection_plan)
    if designation is None:
        return None

    if publish_candidate:
        raise CsvExcelImportError(
            "entity_reference_publication_is_the_import",
            "a reference import publishes its MDM versions as it lands; the "
            "governed warehouse dispatch has no candidate to promote",
            repair={"run_without_governed_dispatch": True},
        )

    kind = designation["object_kind"]
    lookup = fetch_entity_type_lookup(conn, project_id=project_id, object_kinds=[kind])
    issues = validate_entity_designations(mapping_payload, declared_types=lookup)
    if issues:
        first = issues[0]
        raise CsvExcelImportError(
            "entity_reference_designation_invalid",
            first.message,
            repair={"designation_reason": first.reason, "resolve_designation": True},
        )
    registry = fetch_registry(conn, project_id=project_id, object_kind=kind)
    if registry is None:  # pragma: no cover - the lookup above just found it.
        raise CsvExcelImportError(
            "entity_reference_designation_invalid",
            f"entity type {kind!r} is not declared in this Project",
            repair={"resolve_designation": True},
        )
    return {**designation, "registry": registry}


# ---------------------------------------------------------------------------
# Per-row validation. One rule: a row that cannot name its entity is rejected
# with evidence, never dropped (AC4). Runs INSIDE the governed mapping's own
# enumeration (the ``row_validator`` hook of ``_apply_governed_mapping``), so
# its line numbers and the type-coercion rejections' line numbers come from
# the SAME walk over the file.
# ---------------------------------------------------------------------------


def reference_row_validator(route: Mapping[str, Any]) -> Callable[[dict, int], RejectedRow | None]:
    """The key validator for one resolved route, as a mapping row hook.

    Returned as a closure so ``_apply_governed_mapping`` stays route-agnostic:
    it asks "may this row land?" and records the answer, without knowing what
    a reference file is.
    """
    key_field = str(route["field_id"])

    def validate(row: dict[str, Any], row_number: int) -> RejectedRow | None:
        raw = row.get(key_field)
        text = "" if raw is None else str(raw).strip()
        if not text:
            return RejectedRow(
                row_number=row_number,
                field_name=key_field,
                rule=RULE_KEY_MISSING,
                reason="the row names no entity key; a reference row without "
                "its key describes nothing",
                rejected_value=_truncate_value(raw),
            )
        if len(text) > _LABEL_MAX:
            return RejectedRow(
                row_number=row_number,
                field_name=key_field,
                rule=RULE_KEY_TOO_LONG,
                reason=f"the entity key exceeds {_LABEL_MAX} characters; it "
                "becomes the node's label and the store refuses longer",
                rejected_value=_truncate_value(raw),
            )
        return None

    return validate


# ---------------------------------------------------------------------------
# The landing. Rows already typed by the governed mapping become node
# versions, through the ONLY writers master data has.
# ---------------------------------------------------------------------------


def project_org_id(conn, project_id: str) -> str:
    """The org a Project belongs to -- the scope every MDM write requires."""
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    if row is None or not row[0]:
        raise CsvExcelImportError(
            "entity_reference_project_missing",
            "the import's project does not exist; the reference route cannot "
            "scope a single write",
        )
    return str(row[0])


def _json_value(value: Any) -> Any:
    """Coerce a typed cell to a jsonb-safe attribute value.

    ``master_data.canonical_json`` has no default handler -- a Decimal or a
    date in the payload would fail the version write, not the row. Numbers
    stay numbers (the 239/241 attribute projection casts them), dates become
    ISO strings, anything unknown becomes its string form rather than killing
    the import.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def build_node_payload(
    row: Mapping[str, Any],
    *,
    key_target: str,
    canonical_key: str,
    attribute_targets: list[str],
) -> dict[str, Any]:
    """One row's ``payload`` for ``create_node_version``: the attributes bag.

    The key is stored under the registry's DECLARED ``canonical_key`` name --
    the governed identity field (68.1) -- whatever the column's target is
    called. If another column's target collides with that name, the key wins:
    two answers to "what identifies this entity" is one answer too many.
    """
    attributes: dict[str, Any] = {}
    for target in attribute_targets:
        if target == canonical_key or target == "grain_key":
            continue
        attributes[target] = _json_value(row.get(target))
    attributes[canonical_key] = str(row.get(key_target)).strip()
    return {"attributes": attributes}


#: Comment un type physique de mapping devient un type JSON Schema. La table est
#: celle de `_MAPPING_TYPE_ALIASES` (import_landing) lue dans l'autre sens : les
#: memes mots, la meme signification, jamais un second vocabulaire.
_JSON_TYPE_BY_PHYSICAL = {
    "integer": "number",
    "int": "number",
    "int64": "number",
    "decimal": "number",
    "float": "number",
    "float64": "number",
    "number": "number",
    "boolean": "boolean",
    "bool": "boolean",
}


def property_schema(route: Mapping[str, Any], *, canonical_key: str) -> dict[str, Any]:
    """Le schema de proprietes que ce fichier declare. Pur.

    POURQUOI LE TYPE D'ENTITE EN A BESOIN, ET POURQUOI CE N'ETAIT PAS LA.
    Un type d'entite declare (68.1) est un REGISTRE ; la version de TYPE, elle,
    porte le `property_schema` -- la liste des champs qu'une regle a le droit de
    lire. Mesure en pilotant la chaine entiere (story 69.5) : une entite nourrie
    par un fichier n'en avait aucune, donc publier une regle sur elle etait
    refuse par `NoTypeVersion`. Le parcours que l'epic promet -- j'importe mon
    catalogue, puis je classe mes entites avec MA regle -- ne pouvait pas se
    terminer.

    Les types viennent du MAPPING EPINGLE, pas des valeurs : deviner depuis la
    premiere valeur non nulle marche jusqu'au fichier dont la premiere ligne est
    vide. Un type physique inconnu devient `string`, ce que le magasin accepte
    de toute facon.
    """
    properties: dict[str, Any] = {
        canonical_key: {"type": "string"},
    }
    types = route.get("attribute_types") or {}
    for target in route.get("attribute_targets") or ():
        if target in (canonical_key, "grain_key"):
            continue
        physical = str(types.get(target) or "").strip().lower()
        properties[target] = {"type": _JSON_TYPE_BY_PHYSICAL.get(physical, "string")}
    return {"type": "object", "properties": properties}


def version_digest(
    node_id: str, payload: Mapping[str, Any], *, type_version_id: str | None = None
) -> str:
    """The identity ``create_node_version`` would compute for this content.

    Mirrors its digest input EXACTLY -- node, payload, and the two version
    pins. `type_version_id` is a PARAMETER and not a hard-coded None: the route
    pins the type version it just ensured (see `land_entity_reference_rows`),
    and a digest computed without it would differ from the stored one on every
    single entity, so the per-entity no-op would mint a duplicate version at
    each re-import. Measured on the changed-snapshot test the moment the pin
    was added.

    Import provenance stays OUTSIDE the digest by design (migration 295): the
    same attributes carried by a new execution are the same content.
    """
    return content_hash(
        {
            "node_id": node_id,
            "payload": dict(payload),
            "type_version_id": type_version_id,
            "vocabulary_version_id": None,
        }
    )


def _current_key_index(
    conn, *, org_id: str, registry_id: str, canonical_key: str
) -> dict[str, dict[str, Any]]:
    """key -> {node_id, content_hash} for every CURRENT version of a registry.

    The key is read from the payload's declared ``canonical_key`` attribute --
    the governed identity, never the mutable node label: an operator renaming
    a node must not fork its identity, and a re-import reconciles on the key
    the type declared. Nodes this route creates always carry the key; a node
    written by another door without it simply has no entry here (the matching
    epic 68.3 owns cross-door identity, this route does not guess).
    """
    index: dict[str, dict[str, Any]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT node_id, payload, content_hash
            FROM app.master_data_object_versions
            WHERE org_id = %s AND registry_id = %s AND status = 'current'
              AND node_id IS NOT NULL
            """,
            (org_id, registry_id),
        )
        for node_id, payload, digest in cur.fetchall():
            if isinstance(payload, str):  # pragma: no cover - driver-dependent
                payload = json.loads(payload)
            key = ((payload or {}).get("attributes") or {}).get(canonical_key)
            if key is None:
                continue
            index[str(key)] = {"node_id": str(node_id), "content_hash": str(digest)}
    return index


def land_entity_reference_rows(
    conn,
    *,
    org_id: str,
    project_id: str,
    registry: Mapping[str, Any],
    key_target: str,
    attribute_targets: list[str],
    rows: list[dict[str, Any]],
    actor: str,
    execution_id: str,
    mapping_version_id: str,
    ledger_id: str,
    datastream_id: str,
    attribute_types: Mapping[str, str] | None = None,
    effective_date: date | None = None,
) -> dict[str, Any]:
    """Land mapped reference rows as versioned MDM attributes.

    One pass over the rows, three outcomes per entity:

    * first sight of the key -- ``create_node`` then a version, published;
    * known key, changed attributes -- a new version APPENDED and published;
      the prior chain is untouched (as-of reads keep answering);
    * known key, identical content digest -- NOTHING is minted. The per-entity
      no-op is the mechanism that makes a changed snapshot cheap and an
      unchanged row honest.

    Every minted version carries the import's provenance (execution, pinned
    mapping, ledger row) in ``import_provenance`` -- outside the content
    identity, so the no-op above stays provable across executions.

    Runs on the caller's transaction, inside the caller's savepoint: a
    rejection-gate refusal after this returns takes every write back with one
    ``ROLLBACK TO SAVEPOINT``, the way the warehouse route discards a
    candidate.
    """
    registry_id = str(registry["id"])
    canonical_key = str(registry.get("canonical_key") or key_target)
    provenance = {
        "execution_id": execution_id,
        "mapping_version_id": mapping_version_id,
        "ledger_id": ledger_id,
        "datastream_id": datastream_id,
    }
    effective = effective_date or datetime.now(timezone.utc).date()

    # LE TYPE D'ENTITE APPREND CE QUE LE FICHIER DECLARE. Sans version de type,
    # aucune regle ne peut etre publiee sur ces entites (`NoTypeVersion`, story
    # 68.6) -- mesure en pilotant la chaine entiere. `ensure_type_version` est
    # content-adresse : le meme fichier reimporte resout la meme version, il
    # n'en mint pas une seconde.
    type_version = ensure_type_version(
        conn,
        org_id=org_id,
        object_kind=str(registry["object_kind"]),
        label=str(registry.get("display_name") or registry["object_kind"]),
        property_schema=property_schema(
            {
                "attribute_targets": attribute_targets,
                "attribute_types": attribute_types or {},
            },
            canonical_key=canonical_key,
        ),
        actor=actor,
    )

    index = _current_key_index(
        conn, org_id=org_id, registry_id=registry_id, canonical_key=canonical_key
    )

    nodes_created = 0
    versions_minted = 0
    entities_unchanged = 0
    for row in rows:
        key = str(row.get(key_target) or "").strip()
        if not key:
            # The row_validator refused these upstream; one reaching here is a
            # bug in the route, and a silent skip would hide it.
            raise MasterDataError(
                f"a reference row reached the landing without its {key_target!r} key"
            )
        payload = build_node_payload(
            row,
            key_target=key_target,
            canonical_key=canonical_key,
            attribute_targets=attribute_targets,
        )
        current = index.get(key)
        if current is None:
            node = create_node(
                conn,
                org_id=org_id,
                project_id=project_id,
                registry_id=registry_id,
                node_kind=str(registry["object_kind"]),
                label=key,
                actor=actor,
            )
            node_id = str(node["id"])
            nodes_created += 1
        else:
            node_id = current["node_id"]
            if (
                version_digest(
                    node_id, payload, type_version_id=str(type_version["id"])
                )
                == current["content_hash"]
            ):
                entities_unchanged += 1
                continue

        version = create_node_version(
            conn,
            org_id=org_id,
            registry_id=registry_id,
            node_id=node_id,
            payload=payload,
            actor=actor,
            effective_date=effective,
            import_provenance=provenance,
            type_version_id=str(type_version["id"]),
        )
        publish_node_version(conn, org_id=org_id, version_id=str(version["id"]), actor=actor)
        versions_minted += 1
        # A second row for the same key in one file is already impossible (the
        # grain collision guard), so the index only ever needs the first write;
        # updating it keeps the function correct if that guard ever widens.
        index[key] = {"node_id": node_id, "content_hash": version["content_hash"]}

    return {
        "rows": len(rows),
        "nodes_created": nodes_created,
        "versions_minted": versions_minted,
        "entities_unchanged": entities_unchanged,
        "table": f"{LANDING_RELATION_PREFIX}{registry_id}",
        "backend": ROUTE_ENTITY_REFERENCE,
        "registry_id": registry_id,
        "object_kind": str(registry["object_kind"]),
        "execution_id": execution_id,
    }
