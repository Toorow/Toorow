"""Story 66.1 -- the MDM common key: one business identity, declared once.

WHAT THIS OWNS, AND WHAT IT DELIBERATELY DOES NOT. Crossing two Datastreams
needs three facts, and this module owns exactly the first:

    these canonical fields are ONE business identity   <- here
    this physical column IS that canonical field       <- datastream mapping
    the cross goes this way, at this cardinality       <- semantic view relationship

The distinction is not academic. `Day + Campaign` says two sources speak about
the same day and the same campaign; it does NOT say one row of one meets one row
of the other. A module that conflated the two would let a declaration of meaning
authorize an execution -- which is precisely what `governance.md` refuses:
"a key without an approved relationship remains non-executable".

IDENTITY IS THE ORDERED LIST, NOT THE NAME. `[Day, Campaign]` and
`[Campaign, Day]` are two different keys and their `content_hash` says so. A
name is what a human reads in a suggestion; it is never what a relationship
pins. Relationships pin a VERSION id.

NO SECOND VOCABULARY. Components are `app.mdm_canonical_fields.id` resolved
through `canonical_field_registry.list_visible_canonical_fields` -- the same
predicate (`project_id IS NULL OR project_id = %s`) that
`datastream_field_mapping` accepts a binding against. A field this module would
accept is a field a binding can name, because both read one function.

NO STORED COVERAGE. Which Datastreams implement a component is DERIVED from the
published mapping versions at read time. A stored figure would be wrong the next
time a Datastream publishes, and it would be wrong silently.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ulid import ULID

from core.audit import declare_action, insert_audit_row
from core.canonical_field_registry import list_visible_canonical_fields
from core.datastream_field_mapping import physical_type_class

# ---------------------------------------------------------------------------
# What this module writes to the audit trail -- AD-42: an action is declared
# where it is WRITTEN, never in a central list nobody owns.
# ---------------------------------------------------------------------------
#
# AUDIT MDM & GOUVERNANCE, 2026-08-14 : publier une Vue semantique laissait une
# trace, declarer ou archiver l'IDENTITE que cette vue croise n'en laissait
# aucune. Or c'est la decision qui rend un croisement possible : `[Day, Campaign]`
# dit que deux sources parlent de la meme chose, et personne ne pouvait dire qui
# l'avait dit ni quand.
#
# Le journal n'est pas la version : les versions sont deja immuables et gardees
# par un trigger. Il porte le GESTE et son auteur, ce que la table ne stocke que
# pour la creation (`created_by`) et pas du tout pour l'archivage.
ACTION_COMMON_KEY_DECLARED = declare_action("mdm.common_key.declared")
ACTION_COMMON_KEY_VERSIONED = declare_action("mdm.common_key.versioned")
ACTION_COMMON_KEY_ARCHIVED = declare_action("mdm.common_key.archived")


def _record(conn, action: str, *, actor: str, project_id: str, **metadata: Any) -> None:
    """Une ligne de journal, SUR LA MEME TRANSACTION que ce qu'elle enregistre.

    `insert_audit_row` plutot que `write_audit_row` : le second ouvre sa propre
    connexion et ne leve jamais, ce qui convient a un appelant qui a deja
    commite. Ici l'appelant ne l'a pas fait -- `mdm_common_keys_api` appelle puis
    `conn.commit()` -- donc un journal hors transaction affirmerait une
    declaration qu'un rollback aurait effacee. Le geste et sa preuve commitent
    ensemble ou pas du tout.
    """
    insert_audit_row(
        conn,
        identity=actor or "anonymous",
        action=action,
        provider_account="",
        connection_ref="",
        metadata={"project_id": project_id, **metadata},
    )

#: A key component must be exact-equality comparable across two producers.
#:
#: `decimal`, `money`, `ratio`, `percent` and `duration` are excluded because a
#: float computed by two systems does not equal a float computed by two other
#: systems, and a join on one silently drops nearly every row rather than failing.
#: `timestamp` is excluded because it would join at a grain nobody declared --
#: the ratified rule is a DATE everywhere, and an instant carries a timezone this
#: layer has no authority to reconcile.
KEYABLE_VALUE_TYPES: frozenset[str] = frozenset({"string", "integer", "date", "boolean"})

#: A composite key beyond this is not a key, it is a row identity. Bounded here
#: AND in the CHECK of migration 258, so neither door can be the wide one.
MAX_COMPONENTS = 8

#: Binding statuses that count as an implementation of a component. The same
#: reading `datastream_field_mapping.py:644` uses to project a metric: a
#: `suggested` or `blocking` binding is a proposal, not a physical fact.
IMPLEMENTING_BINDING_STATUSES: frozenset[str] = frozenset({"confirmed", "resolved"})

#: Bumped when the hashed document changes shape, so a stored hash is only ever
#: compared to a hash produced by the same contract.
COMMON_KEY_CONTRACT_VERSION = "mdm-common-key.v1"


class CommonKeyRefused(ValueError):
    """A refusal with a code a caller can act on, and a sentence a human reads."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CommonKeyNotFound(LookupError):
    """Foreign, denied and nonexistent are ONE answer.

    Telling them apart tells an unauthorized caller that the object exists, so
    every read that cannot serve raises this and the route answers 404. The
    distinguishing reason belongs in audit.
    """


@dataclass(frozen=True)
class Component:
    """One resolved, ordered component of a key version."""

    ordinal: int
    canonical_field_id: str
    canonical_name: str
    value_type: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "canonical_field_id": self.canonical_field_id,
            "canonical_name": self.canonical_name,
            "value_type": self.value_type,
        }


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def components_hash(components: Sequence[Component]) -> str:
    """The identity of a version: the contract version and the ORDERED ids.

    The names are deliberately absent from the hash. Renaming a canonical field
    does not change which business identity a key expresses, and a hash that
    moved on a rename would break every relationship that pinned the version.
    """
    document = {
        "contract": COMMON_KEY_CONTRACT_VERSION,
        "components": [component.canonical_field_id for component in components],
    }
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def clean_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CommonKeyRefused("name_required", "A common key needs a name.")
    name = value.strip()
    if len(name) > 120:
        raise CommonKeyRefused(
            "name_too_long", "A common key name is at most 120 characters."
        )
    return name


def resolve_components(
    conn, *, project_id: str, canonical_field_ids: Sequence[Any]
) -> list[Component]:
    """Turn an ordered list of canonical field ids into validated components.

    Every refusal happens HERE, before any write, and every one of them names
    the component it rejected. A refusal that said only "invalid components"
    would leave a caller with eight candidates and no way to repair.
    """
    if not isinstance(canonical_field_ids, (list, tuple)) or not canonical_field_ids:
        raise CommonKeyRefused(
            "components_required",
            "A common key declares at least one canonical dimension.",
        )
    if len(canonical_field_ids) > MAX_COMPONENTS:
        raise CommonKeyRefused(
            "too_many_components",
            f"A common key carries at most {MAX_COMPONENTS} components; "
            f"{len(canonical_field_ids)} were sent.",
        )

    wanted: list[str] = []
    for raw in canonical_field_ids:
        if not isinstance(raw, str) or not raw.strip():
            raise CommonKeyRefused(
                "component_not_found",
                "Every component is a canonical field id.",
            )
        field_id = raw.strip()
        if field_id in wanted:
            raise CommonKeyRefused(
                "component_duplicated",
                f"{field_id} appears twice in the same key; a component is named once.",
            )
        wanted.append(field_id)

    # The SAME visibility predicate a binding is validated against, read from the
    # one function that owns it. `list_visible_canonical_fields` returns only
    # ACTIVE rows, at both scopes.
    visible = {row["id"]: row for row in list_visible_canonical_fields(conn, project_id=project_id)}

    components: list[Component] = []
    for ordinal, field_id in enumerate(wanted):
        row = visible.get(field_id)
        if row is None:
            # Unknown, archived and foreign are one answer on purpose: a caller
            # of another project must not learn that an id exists elsewhere.
            raise CommonKeyRefused(
                "component_not_found",
                f"{field_id} is not an active canonical field of this project.",
            )
        if row.get("concept_kind") != "dimension":
            raise CommonKeyRefused(
                "component_is_metric",
                f"{row.get('canonical_name')} is a measure. A key names what two "
                "sources have in common, and a measure is what they report.",
            )
        value_type = str(row.get("value_type") or "")
        if value_type not in KEYABLE_VALUE_TYPES:
            raise CommonKeyRefused(
                "component_not_keyable:" + (value_type or "unknown"),
                f"{row.get('canonical_name')} is a {value_type or 'field of unknown type'} "
                "and cannot be matched exactly across two sources. Keyable types are "
                + ", ".join(sorted(KEYABLE_VALUE_TYPES))
                + ".",
            )
        components.append(
            Component(
                ordinal=ordinal,
                canonical_field_id=field_id,
                canonical_name=str(row.get("canonical_name") or ""),
                value_type=value_type,
            )
        )
    _refuse_disagreeing_physical_types(
        conn, project_id=project_id, components=components
    )
    return components


#: Two physical types AGREE when the one classifier the repository owns puts them
#: in the same class. Comparing the raw strings would refuse `STRING` against
#: `VARCHAR` -- the same thing spelled by two warehouses -- and that refusal would
#: teach nobody anything. `unknown` is NOT a class here: a type the classifier
#: does not recognize is an `unknown_field` ambiguity the mapping already raises,
#: and it is not evidence that two sources disagree.
def _refuse_disagreeing_physical_types(
    conn, *, project_id: str, components: Sequence[Component]
) -> None:
    """Refuse a component whose implementing physical types disagree.

    `governance.md:1554`: "a component whose implementing physical types
    disagree is refused by name". The comparison is made HERE, before any write,
    for the same reason every other component refusal is: a key declared over a
    column that is a string in one Datastream and an integer in another says two
    sources share an identity they cannot match on.

    A read that FAILED does not refuse. `mapping_coverage` already separates "I
    could not look" from "nobody binds it", and declaring an identity is not
    executing a cross -- the relationship that pins this version is where an
    execution is authorized, and it reads the mappings again.
    """
    coverage = mapping_coverage(
        conn,
        project_id=project_id,
        components=[
            {
                "canonical_field_id": component.canonical_field_id,
                "canonical_name": component.canonical_name,
            }
            for component in components
        ],
    )
    if coverage.get("state") != "available":
        return

    for entry in coverage.get("components") or []:
        by_class: dict[str, list[str]] = {}
        for implementation in entry.get("implemented_by") or []:
            physical_type = str(implementation.get("physical_type") or "")
            klass = physical_type_class(physical_type, "")
            if klass == "unknown":
                continue
            by_class.setdefault(klass, []).append(
                f"{implementation.get('datastream_name')} "
                f"({physical_type or 'untyped'}, {klass})"
            )
        if len(by_class) < 2:
            continue
        named = "; ".join(
            sorted(item for values in by_class.values() for item in values)
        )
        # LE NOM, OU RIEN. Le repli sur `canonical_field_id` faisait commencer un
        # refus par une identite -- `test_no_refusal_anywhere_in_core_starts_-
        # rendering_an_identifier` l'attrape, et il a raison : la personne qui lit
        # cette phrase repare un mapping, elle ne cherche pas une cle primaire.
        # Un champ sans nom canonique se designe par ce qu'il EST, et la phrase
        # suivante nomme deja les colonnes qui divergent.
        subject = str(entry.get("canonical_name") or "").strip() or (
            "A component of this key"
        )
        raise CommonKeyRefused(
            "component_physical_types_disagree",
            f"{subject} is implemented by columns of disagreeing physical types — "
            f"{named}. Two sources cannot be matched on a column one stores as one "
            "type and the other as another; repair the mapping that is wrong, then "
            "declare the key.",
        )


def _project_org(conn, project_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    if row is None:
        raise CommonKeyNotFound(project_id)
    return str(row[0])


def create_common_key(
    conn,
    *,
    project_id: str,
    name: Any,
    canonical_field_ids: Sequence[Any],
    actor: str,
    description: str | None = None,
) -> dict[str, Any]:
    """Create the key and its immutable version 1. The caller owns the transaction."""
    key_name = clean_name(name)
    components = resolve_components(
        conn, project_id=project_id, canonical_field_ids=canonical_field_ids
    )
    org_id = _project_org(conn, project_id)

    key_id = f"mck_{ULID()}"
    version_id = f"mckv_{ULID()}"
    payload = [component.as_dict() for component in components]

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.mdm_common_keys
                (id, org_id, project_id, name, description, created_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (key_id, org_id, project_id, key_name, description, actor),
        )
        if cur.fetchone() is None:
            # The partial unique index on the active name refused it. Saying the
            # name back is the difference between a repairable answer and a
            # constraint name.
            raise CommonKeyRefused(
                "common_key_name_taken",
                f"{key_name!r} is already an active common key in this project.",
            )
        cur.execute(
            """
            INSERT INTO app.mdm_common_key_versions
                (id, common_key_id, org_id, project_id, version_number, components,
                 content_hash, created_by)
            VALUES (%s, %s, %s, %s, 1, %s::jsonb, %s, %s)
            """,
            (
                version_id,
                key_id,
                org_id,
                project_id,
                canonical_json(payload),
                components_hash(components),
                actor,
            ),
        )
        cur.execute(
            "UPDATE app.mdm_common_keys SET current_version_id = %s, updated_at = NOW() "
            "WHERE id = %s AND project_id = %s",
            (version_id, key_id, project_id),
        )

    _record(
        conn,
        ACTION_COMMON_KEY_DECLARED,
        actor=actor,
        project_id=project_id,
        common_key_id=key_id,
        name=key_name,
        version_id=version_id,
        version_number=1,
        content_hash=components_hash(components),
        components=[component.canonical_field_id for component in components],
    )

    return {
        "id": key_id,
        "name": key_name,
        "description": description,
        "status": "active",
        "current_version": {
            "id": version_id,
            "version_number": 1,
            "content_hash": components_hash(components),
            "components": payload,
        },
    }


def append_version(
    conn,
    *,
    project_id: str,
    common_key_id: str,
    canonical_field_ids: Sequence[Any],
    actor: str,
) -> dict[str, Any]:
    """Append version N+1 and move the head. Never edits version N."""
    head = _load_key_row(conn, project_id=project_id, common_key_id=common_key_id)
    if head["status"] != "active":
        # NAME A GESTURE THAT EXISTS. This sentence used to read "Reactivate it
        # first", and no reactivation route has ever existed -- the refusal sent
        # a person looking for a control nobody had built (audit 2026-08-14).
        #
        # Declaring again is not a workaround, it is what the schema allows:
        # `uq_mdm_common_keys_active_name` (migration 258) is UNIQUE only WHERE
        # `status = 'active'`, so an archived name is free to be taken again. The
        # new key starts at version 1 with its own id, which is the honest
        # outcome anyway: relationships pin a VERSION, and resurrecting the old
        # key would silently hand them back a history they were detached from.
        raise CommonKeyRefused(
            "common_key_archived",
            "This common key is archived and does not take new versions. Declare a "
            "key with the same name — an archived name is free again — then pin it "
            "on the relationships that need it.",
        )
    components = resolve_components(
        conn, project_id=project_id, canonical_field_ids=canonical_field_ids
    )
    new_hash = components_hash(components)
    if head.get("current_content_hash") == new_hash:
        raise CommonKeyRefused(
            "common_key_unchanged",
            "This version declares the same components, in the same order, as the "
            "current one. Nothing would change, and the relationships that pin the "
            "current version would gain a twin.",
        )

    version_id = f"mckv_{ULID()}"
    payload = [component.as_dict() for component in components]
    next_number = int(head["current_version_number"] or 0) + 1

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.mdm_common_key_versions
                (id, common_key_id, org_id, project_id, version_number, components,
                 content_hash, created_by)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (
                version_id,
                common_key_id,
                head["org_id"],
                project_id,
                next_number,
                canonical_json(payload),
                new_hash,
                actor,
            ),
        )
        cur.execute(
            "UPDATE app.mdm_common_keys SET current_version_id = %s, updated_at = NOW() "
            "WHERE id = %s AND project_id = %s",
            (version_id, common_key_id, project_id),
        )

    _record(
        conn,
        ACTION_COMMON_KEY_VERSIONED,
        actor=actor,
        project_id=project_id,
        common_key_id=common_key_id,
        name=head["name"],
        version_id=version_id,
        version_number=next_number,
        content_hash=new_hash,
        replaces_content_hash=head.get("current_content_hash"),
        components=[component.canonical_field_id for component in components],
    )

    return {
        "id": common_key_id,
        "current_version": {
            "id": version_id,
            "version_number": next_number,
            "content_hash": new_hash,
            "components": payload,
        },
    }


def _load_key_row(conn, *, project_id: str, common_key_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT k.id, k.org_id, k.project_id, k.name, k.description, k.status,
                   k.current_version_id, v.version_number, v.content_hash, v.components,
                   k.created_by, k.created_at, k.updated_at
              FROM app.mdm_common_keys k
              LEFT JOIN app.mdm_common_key_versions v ON v.id = k.current_version_id
             WHERE k.id = %s AND k.project_id = %s
            """,
            (common_key_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise CommonKeyNotFound(common_key_id)
    return {
        "id": row[0],
        "org_id": row[1],
        "project_id": row[2],
        "name": row[3],
        "description": row[4],
        "status": row[5],
        "current_version_id": row[6],
        "current_version_number": row[7],
        "current_content_hash": row[8],
        "current_components": row[9] or [],
        "created_by": row[10],
        "created_at": row[11],
        "updated_at": row[12],
    }


def list_common_keys(conn, *, project_id: str) -> list[dict[str, Any]]:
    """Every common key of the project, current version first-class."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT k.id, k.name, k.description, k.status, v.id, v.version_number,
                   v.content_hash, v.components, k.updated_at
              FROM app.mdm_common_keys k
              LEFT JOIN app.mdm_common_key_versions v ON v.id = k.current_version_id
             WHERE k.project_id = %s
             ORDER BY k.status, lower(k.name)
            """,
            (project_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "id": row[0],
            "name": row[1],
            "description": row[2],
            "status": row[3],
            "current_version": {
                "id": row[4],
                "version_number": row[5],
                "content_hash": row[6],
                "components": row[7] or [],
            }
            if row[4]
            else None,
            "component_count": len(row[7] or []),
            "updated_at": row[8].isoformat() if row[8] is not None else None,
        }
        for row in rows
    ]


def mapping_coverage(
    conn, *, project_id: str, components: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Which published Datastream mappings implement each component, derived now.

    THREE STATES, AND THEY ARE NOT THE SAME FACT. A Datastream whose published
    mapping binds the component is `implemented`. A Datastream that publishes NO
    mapping version is `unknown` -- it is never counted as covered, the rule
    `governance.md:84` already states for concept coverage. And a read that
    FAILED returns `unavailable` with no counts at all: "I could not look" and
    "nobody binds it" are different answers, and only one of them invites a
    person to go and map something.
    """
    component_list = [dict(component) for component in components]
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.id, d.name, d.current_mapping_version_id, m.mapping_payload
                  FROM app.datastreams d
                  LEFT JOIN app.datastream_mapping_versions m
                         ON m.id = d.current_mapping_version_id
                 WHERE d.project_id = %s
                 ORDER BY lower(d.name)
                """,
                (project_id,),
            )
            rows = cur.fetchall()
    except Exception:  # pragma: no cover - exercised through the API's 503 path
        return {"state": "unavailable", "components": [], "unmapped_datastreams": None}

    published: list[tuple[str, str, str, dict[str, Any]]] = []
    unknown: list[dict[str, str]] = []
    for datastream_id, name, mapping_version_id, payload in rows:
        if not mapping_version_id or not isinstance(payload, dict):
            unknown.append({"id": datastream_id, "name": name})
            continue
        published.append((datastream_id, name, mapping_version_id, payload))

    per_component: list[dict[str, Any]] = []
    for component in component_list:
        field_id = str(component.get("canonical_field_id") or "")
        implementations: list[dict[str, str]] = []
        for datastream_id, name, mapping_version_id, payload in published:
            for field in payload.get("fields") or []:
                if not isinstance(field, dict):
                    continue
                binding = field.get("binding")
                if not isinstance(binding, dict):
                    continue
                if binding.get("mdm_target") != field_id:
                    continue
                if binding.get("status") not in IMPLEMENTING_BINDING_STATUSES:
                    continue
                implementations.append(
                    {
                        "datastream_id": datastream_id,
                        "datastream_name": name,
                        "mapping_version_id": mapping_version_id,
                        "physical_field_id": str(field.get("field_id") or ""),
                        # Carried because the component refusal above compares it.
                        # Reading the mappings a second time would be a second
                        # answer free to diverge from this one.
                        "physical_type": str(field.get("physical_type") or ""),
                    }
                )
                break
        per_component.append(
            {
                "canonical_field_id": field_id,
                "canonical_name": component.get("canonical_name"),
                "implemented_by": implementations,
                "implementation_count": len(implementations),
            }
        )

    return {
        "state": "available",
        "components": per_component,
        # Named on its own row, never folded into a fraction: a coverage of 1/1
        # beside eight unmapped Datastreams must not read as a finished project.
        "unmapped_datastreams": unknown,
    }


#: A Semantic View version whose relationships can still come into force. A
#: `draft` or a `candidate` is not permission to execute anything -- that rule is
#: `datastream_matches._executable_key_versions`' and it stands -- but it CAN be
#: published tomorrow, so a key it names is not free to be retired today.
#:
#: `superseded` and `archived` are the opposite: they are history, and history
#: never comes back. Blocking on them was a defect measured 2026-08-16 -- a key
#: was refused retirement because a view version nobody can revive once named it.
LIVE_VIEW_STATUSES = frozenset({"draft", "candidate", "published"})


def used_by(conn, *, project_id: str, common_key_id: str) -> list[dict[str, Any]]:
    """The Semantic View relationships that pin an exact version of this key.

    EVERY pin is returned, history included, and each one says the LIFECYCLE of
    the view version it lives in. The read does not decide: a surface that wants
    to show what still holds filters on :data:`LIVE_VIEW_STATUSES`, and one that
    wants the whole story has it. Filtering here would have made the two
    impossible to tell apart -- which is what this function did until 2026-08-16,
    when it returned a bare count that a caller could only read as "in use".
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.view_version_id, r.name, r.ordinal, v.version_number,
                   sv.view_id, r.mdm_common_key_version_id, sv.status, s.name
              FROM app.semantic_view_version_relationships r
              JOIN app.mdm_common_key_versions v ON v.id = r.mdm_common_key_version_id
              JOIN app.semantic_view_versions sv ON sv.id = r.view_version_id
              JOIN app.semantic_views s ON s.id = sv.view_id
             WHERE v.common_key_id = %s AND v.project_id = %s
             ORDER BY r.view_version_id, r.ordinal
            """,
            (common_key_id, project_id),
        )
        rows = cur.fetchall()
    return [
        {
            "view_version_id": row[0],
            "relationship_name": row[1],
            "ordinal": row[2],
            "key_version_number": row[3],
            "view_id": row[4],
            "key_version_id": row[5],
            "view_status": row[6],
            "view_name": row[7],
            #: Derived, never stored: what a reader actually needs to decide.
            "still_holds": str(row[6]) in LIVE_VIEW_STATUSES,
        }
        for row in rows
    ]


def read_common_key(conn, *, project_id: str, common_key_id: str) -> dict[str, Any]:
    """One key, its versions, its derived Mapping Coverage and its Used by."""
    head = _load_key_row(conn, project_id=project_id, common_key_id=common_key_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, version_number, content_hash, components, created_by, created_at
              FROM app.mdm_common_key_versions
             WHERE common_key_id = %s AND project_id = %s
             ORDER BY version_number DESC
            """,
            (common_key_id, project_id),
        )
        version_rows = cur.fetchall()

    return {
        "id": head["id"],
        "name": head["name"],
        "description": head["description"],
        "status": head["status"],
        "current_version": {
            "id": head["current_version_id"],
            "version_number": head["current_version_number"],
            "content_hash": head["current_content_hash"],
            "components": head["current_components"],
        }
        if head["current_version_id"]
        else None,
        "versions": [
            {
                "id": row[0],
                "version_number": row[1],
                "content_hash": row[2],
                "components": row[3] or [],
                "created_by": row[4],
                "created_at": row[5].isoformat() if row[5] is not None else None,
            }
            for row in version_rows
        ],
        "mapping_coverage": mapping_coverage(
            conn, project_id=project_id, components=head["current_components"] or []
        ),
        "used_by": used_by(conn, project_id=project_id, common_key_id=common_key_id),
    }


def archive_common_key(
    conn, *, project_id: str, common_key_id: str, actor: str
) -> dict[str, Any]:
    """Archive a key, unless an executable path depends on one of its versions."""
    head = _load_key_row(conn, project_id=project_id, common_key_id=common_key_id)
    #  SEULS LES PINS VIVANTS BLOQUENT (mesure du 2026-08-16). `used_by` ne
    #  filtrait aucun statut, donc une relation d une version de vue `superseded`
    #  ou `archived` -- de l HISTOIRE, que rien ne peut ranimer -- refusait
    #  l archivage pour toujours. Le message disait alors << retirez ces
    #  relations >>, en nommant un geste qui n existe pas sur une version figee.
    pinned = [
        entry
        for entry in used_by(conn, project_id=project_id, common_key_id=common_key_id)
        if entry["still_holds"]
    ]
    if pinned:
        #  Un brouillon et une version publiee bloquent tous les deux, et pas pour
        #  la meme raison : l une sert aujourd hui, l autre servira le jour ou
        #  quelqu un la publie. Les compter ensemble sans les nommer envoyait la
        #  personne chercher dans les vues publiees une relation qui n y est pas.
        live = [entry for entry in pinned if entry["view_status"] == "published"]
        pending = [entry for entry in pinned if entry["view_status"] != "published"]
        parts = []
        if live:
            parts.append(f"{len(live)} published")
        if pending:
            parts.append(f"{len(pending)} not yet published")
        where = ", ".join(
            sorted({f"{entry['view_name']} ({entry['view_status']})" for entry in pinned})
        )
        raise CommonKeyRefused(
            "common_key_in_use",
            f"{head['name']!r} is pinned by {len(pinned)} Semantic View relationship"
            + ("s" if len(pinned) != 1 else "")
            + f" ({' and '.join(parts)}), in {where}. Retire those relationships "
            "before archiving the identity they use.",
        )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.mdm_common_keys SET status = 'archived', updated_at = NOW() "
            "WHERE id = %s AND project_id = %s",
            (common_key_id, project_id),
        )
    _record(
        conn,
        ACTION_COMMON_KEY_ARCHIVED,
        actor=actor,
        project_id=project_id,
        common_key_id=common_key_id,
        name=head["name"],
    )
    return {"id": common_key_id, "status": "archived", "archived_by": actor}


# ---------------------------------------------------------------------------
# The Project proposes its shared identities, across every flow (2026-09-04).
# ---------------------------------------------------------------------------

#: A key component is a dimension: a column whose role is a measure is never
#: proposed, whatever its name says about the flows it appears in.
_MEASURE_ROLE_PREFIX = "measure"
#: A proposal is a reading aid, bounded like every other list of the product.
MAX_PROPOSED_IDENTITIES = 50


def shared_identity_name(field: Mapping[str, Any]) -> str:
    """The name under which a column joins a shared identity: its canonical target, else itself."""
    binding = field.get("binding") if isinstance(field.get("binding"), dict) else {}
    target = str(binding.get("canonical_target") or "").strip()
    return target or str(field.get("field_id") or "")


def propose_shared_identities(conn, *, project_id: str) -> dict[str, Any]:
    """Which identities the Project's flows share, and which flows still have to pin them.

    Derived from the CURRENT mapping version of every non-archived Datastream, in
    one read, never stored (`governance.md`, amendment of 2026-09-04). Measured
    the same day on the reference project: ten flows, `date` on all ten and
    `channel_id` on nine, two of them pinned -- and the only place that said so
    was the pairwise match catalogue, thirty-six times over.

    An identity is a column name carried by at least two Datastreams, or a
    canonical field already pinned by at least two. For each, the carriers, the
    ones already pinned (and to what), the ones still to pin, and the canonical
    field proposed: the one the pinned carriers use, else the active registry
    field of the same role and name, else none -- said as such. A Datastream
    that publishes no mapping is `unknown`, listed and never counted.

    MEASURES ARE PROPOSED TOO (amendment of 2026-09-05), as soon as ONE flow
    carries them: a governed measure is what a crossing plan SELECTS, not what
    it joins on, so it needs no twin -- and the crossing gate measured on the
    reference project stopped at « the frozen plan carries no governed measure »
    while seven flows carried `views`. A measure is never proposed as a key
    component; its gesture says « pin it, so the measure is selectable ».
    """
    with conn.cursor() as cur:
        # The CURRENT (published) mapping version decides; the NEWEST version,
        # when it is not the current one, is read beside it because that is
        # exactly the reference project's state on 2026-09-04: pins confirmed in
        # a newer mapping version whose candidate was never published. Saying
        # "publish it" is a different gesture from "pin it".
        cur.execute(
            """
            SELECT d.id, d.name, d.current_mapping_version_id, m.mapping_payload,
                   newest.id, newest.mapping_payload
              FROM app.datastreams d
              LEFT JOIN app.datastream_mapping_versions m
                     ON m.id = d.current_mapping_version_id
              LEFT JOIN LATERAL (
                    SELECT v.id, v.mapping_payload
                      FROM app.datastream_mapping_versions v
                     WHERE v.datastream_id = d.id AND v.project_id = d.project_id
                       AND (d.current_mapping_version_id IS NULL OR v.id <> d.current_mapping_version_id)
                     ORDER BY v.version_number DESC
                     LIMIT 1
              ) newest ON TRUE
             WHERE d.project_id = %s AND d.archived_at IS NULL
             ORDER BY lower(d.name), d.id
            """,
            (project_id,),
        )
        rows = cur.fetchall()
        cur.execute(
            """
            SELECT id, canonical_name, concept_kind, project_id
              FROM app.mdm_canonical_fields
             WHERE status = 'active'
               AND (project_id IS NULL OR project_id = %s)
            """,
            (project_id,),
        )
        registry_rows = [
            (str(field_id), str(name), str(kind), project_scope)
            for field_id, name, kind, project_scope in cur.fetchall()
        ]
    # Every active id keeps its name and kind; for a (ROLE, NAME), the Project's
    # own field wins over the platform's when both exist (the reference project
    # holds its own `date` and `channel_id` beside the platform's -- a dict keyed
    # by name kept one of the two and lost the other's id, measured 2026-09-04).
    # The role is part of the key: a metric column is never proposed the
    # dimension of the same name (2026-09-05).
    registry_names = {field_id: name for field_id, name, _kind, _scope in registry_rows}
    registry_kinds = {field_id: kind for field_id, _name, kind, _scope in registry_rows}
    registry: dict[tuple[str, str], str] = {}
    for field_id, name, kind, project_scope in sorted(registry_rows, key=lambda r: r[3] is None):
        registry.setdefault((kind, name), field_id)

    unknown: list[dict[str, str]] = []
    # (kind, role, identity) -> {carriers: {datastream_id: {...}}}; kind is
    # "column" | "canonical", role is "dimension" | "metric".
    identities: dict[tuple[str, str, str], dict[str, Any]] = {}
    for datastream_id, name, mapping_version_id, payload, newer_id, newer_payload in rows:
        if not mapping_version_id or not isinstance(payload, dict):
            unknown.append({"id": str(datastream_id), "name": str(name)})
            continue
        pending: dict[str, str] = {}
        if newer_id and isinstance(newer_payload, dict):
            for field in newer_payload.get("fields") or []:
                binding = field.get("binding") if isinstance(field, dict) and isinstance(field.get("binding"), dict) else {}
                if binding.get("mdm_target") and binding.get("status") in IMPLEMENTING_BINDING_STATUSES:
                    pending[str(field.get("field_id") or "")] = str(binding["mdm_target"])
        for field in payload.get("fields") or []:
            if not isinstance(field, dict):
                continue
            binding = field.get("binding") if isinstance(field.get("binding"), dict) else {}
            suggestion = field.get("suggestion") if isinstance(field.get("suggestion"), dict) else {}
            if binding.get("status") == "excluded":
                continue
            is_measure = str(suggestion.get("semantic_role") or "").startswith(_MEASURE_ROLE_PREFIX)
            role = "metric" if is_measure else "dimension"
            column = str(field.get("field_id") or "")
            if not column:
                continue
            pinned = str(binding.get("mdm_target") or "") if binding.get("status") in IMPLEMENTING_BINDING_STATUSES else ""
            # ACROSS SOURCES, THE IDENTITY IS THE CANONICAL TARGET, not the raw
            # column name (2026-09-05): the connector catalogues already speak one
            # vocabulary (`canonical_dimension_mapping` -- Search Console `page`
            # and GA4 `pagePath` both mean `page`), and the mapping profile writes
            # it on the binding. Two flows whose columns differ but name the same
            # target are one identity, travelling under two names.
            identity_name = shared_identity_name(field)
            carrier = {
                "datastream_id": str(datastream_id),
                "datastream_name": str(name),
                "mapping_version_id": str(mapping_version_id),
                "column": column,
                "pinned_to": pinned or None,
                # Pinned in a newer, unpublished mapping version: the gesture is
                # to publish it, not to pin it again.
                "pending_in_version": str(newer_id) if (not pinned and pending.get(column)) else None,
                "pending_to": pending.get(column) if not pinned else None,
                "role": role,
                "aggregation": str(suggestion.get("aggregation")) if is_measure and suggestion.get("aggregation") else None,
            }
            identities.setdefault(("column", role, identity_name), {"carriers": {}})["carriers"].setdefault(str(datastream_id), carrier)
            if pinned:
                pinned_role = registry_kinds.get(pinned, role)
                identities.setdefault(("canonical", pinned_role, pinned), {"carriers": {}})["carriers"].setdefault(str(datastream_id), carrier)

    # A column identity and the canonical identity it points at are ONE sentence
    # -- and the carriers NOT yet pinned must survive the merge. Measured
    # 2026-09-05 on the reference project: six flows pinned `views`, the seventh
    # (unpinned) vanished from the proposal because the column identity was
    # dropped whole in favour of the canonical one. Merge the column identity's
    # carriers into the canonical one it means, then drop the column identity.
    for (kind, role, key), entry in list(identities.items()):
        if kind != "column":
            continue
        carriers = list(entry["carriers"].values())
        pinned_targets = {c["pinned_to"] for c in carriers if c["pinned_to"]}
        pending_targets = {c["pending_to"] for c in carriers if c.get("pending_to")}
        if len(pinned_targets) == 1:
            target = next(iter(pinned_targets))
        elif not pinned_targets and len(pending_targets) == 1:
            target = next(iter(pending_targets))
        else:
            target = registry.get((role, key))
        if not target:
            continue
        canonical_key = ("canonical", registry_kinds.get(target, role), target)
        if canonical_key not in identities:
            continue
        for datastream_id, carrier in entry["carriers"].items():
            identities[canonical_key]["carriers"].setdefault(datastream_id, carrier)
        identities.pop((kind, role, key), None)

    proposals: list[dict[str, Any]] = []
    seen_columns_by_canonical: dict[str, set[str]] = {}
    for (kind, role, key), entry in identities.items():
        carriers = list(entry["carriers"].values())
        # A dimension is SHARED when two flows carry it; a measure is governed
        # for what a plan selects, and one carrier is enough (2026-09-05).
        if len(carriers) < (2 if role == "dimension" else 1):
            continue
        pinned_targets = {c["pinned_to"] for c in carriers if c["pinned_to"]}
        if kind == "column":
            # The carriers' own consensus first (published pins, then pins waiting
            # in an unpublished version), the registry field of the same name last:
            # a Project may hold its own `date` beside the platform's, and the one
            # the flows already point at is the one to finish.
            pending_targets = {c["pending_to"] for c in carriers if c.get("pending_to")}
            if len(pinned_targets) == 1:
                canonical_id = next(iter(pinned_targets))
            elif not pinned_targets and len(pending_targets) == 1:
                canonical_id = next(iter(pending_targets))
            else:
                canonical_id = registry.get((role, key))
            canonical_name = registry_names.get(canonical_id or "", key if canonical_id else None)
            if canonical_id and canonical_id not in registry_names:
                # Pinned to a field the registry no longer holds as active here.
                canonical_id, canonical_name = None, None
            identity_name = key
        else:
            canonical_id = key if key in registry_names else None
            canonical_name = registry_names.get(key)
            # THE IDENTITY IS WHAT THE FLOWS CALL IT when they all agree (`date`,
            # pinned to the canonical `day`); the canonical name only when the
            # columns differ (Search Console `page` and GA4 `pagePath`).
            columns = {c["column"] for c in carriers}
            identity_name = next(iter(columns)) if len(columns) == 1 else (canonical_name or key)
            seen_columns_by_canonical[key] = columns
        already = [c for c in carriers if canonical_id and c["pinned_to"] == canonical_id]
        pending_carriers = [
            c for c in carriers
            if not c["pinned_to"] and c.get("pending_to") and (not canonical_id or c["pending_to"] == canonical_id)
        ]
        to_pin = [
            c for c in carriers
            if c not in already and c not in pending_carriers
        ]
        aggregations = {c.get("aggregation") for c in carriers if c.get("aggregation")}
        aggregation = next(iter(aggregations)) if len(aggregations) == 1 else None
        if role == "metric":
            # THE GESTURE OF A MEASURE NEVER SAYS « KEY »: it is what a crossing
            # selects, and a key component is a dimension (2026-08-13).
            if canonical_id and not to_pin and not pending_carriers:
                gesture = (
                    f"Every flow that carries `{identity_name}` already pins it to `{canonical_name}`: "
                    "it is selectable as a measure of every crossing."
                )
            elif canonical_id:
                parts = []
                if pending_carriers:
                    parts.append(
                        f"{len(pending_carriers)} flow(s) already pin `{identity_name}` to `{canonical_name}` in a "
                        "newer mapping version that is not published: publish those versions"
                    )
                if to_pin:
                    parts.append(f"pin `{identity_name}` to `{canonical_name}` on the Mapping of {len(to_pin)} flow(s)")
                gesture = "; ".join(parts) + ", so the measure is selectable in every crossing."
                gesture = gesture[0].upper() + gesture[1:]
            else:
                gesture = (
                    f"No active canonical metric is named `{identity_name}`: register one"
                    + (f" (aggregation `{aggregation}`)" if aggregation else "")
                    + f", or pick the one it means, then pin the {len(carriers)} flow(s) to it."
                )
        elif canonical_id and not to_pin and not pending_carriers:
            gesture = f"Every flow that carries `{identity_name}` already pins it to `{canonical_name}`: declare a common key over it."
        elif canonical_id:
            parts = []
            if pending_carriers:
                parts.append(
                    f"{len(pending_carriers)} flow(s) already pin `{identity_name}` to `{canonical_name}` in a newer "
                    "mapping version that is not published: publish those versions"
                )
            if to_pin:
                parts.append(f"pin `{identity_name}` to `{canonical_name}` on the Mapping of {len(to_pin)} flow(s)")
            gesture = "; ".join(parts) + ", then declare a common key over it."
            gesture = gesture[0].upper() + gesture[1:]
        else:
            gesture = (
                f"No active canonical dimension is named `{identity_name}`: register one, or pick the one it "
                f"means, then pin the {len(carriers)} flow(s) to it."
            )
        proposals.append(
            {
                "identity": identity_name,
                "kind": kind,
                "role": role,
                "aggregation": aggregation if role == "metric" else None,
                "also_known_as": sorted({c["column"] for c in carriers if c["column"] != identity_name}),
                "carrier_count": len(carriers),
                "carriers": carriers,
                "already_pinned": [c["datastream_id"] for c in already],
                "pending_publication": [c["datastream_id"] for c in pending_carriers],
                "to_pin": [c["datastream_id"] for c in to_pin],
                "canonical_field_id": canonical_id,
                "canonical_name": canonical_name,
                "gesture": gesture,
            }
        )
    # A canonical identity and the column identity that pins to it are one
    # sentence, not two: keep the canonical one when the column names it.
    by_canonical = {p["canonical_field_id"] for p in proposals if p["kind"] == "canonical"}
    proposals = [
        p for p in proposals
        if not (p["kind"] == "column" and p["canonical_field_id"] in by_canonical
                and p["identity"] in seen_columns_by_canonical.get(p["canonical_field_id"], set()))
    ]
    # Dimensions first -- they are what a crossing joins on -- then measures,
    # each by how many flows carry them.
    proposals.sort(key=lambda p: (p["role"] != "dimension", -p["carrier_count"], p["identity"]))
    return {
        "state": "available",
        "proposals": proposals[:MAX_PROPOSED_IDENTITIES],
        "truncated": len(proposals) > MAX_PROPOSED_IDENTITIES,
        "unmapped_datastreams": unknown,
        "datastreams_read": len(rows) - len(unknown),
    }
