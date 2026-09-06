"""The writer `app.mdm_canonical_fields` never had (AI-248, arbitration 2026-08-08).

WHAT WAS MISSING, MEASURED. The table is created by migration 032. SIX production
modules READ it -- `datastream_field_mapping` validates every mdm-bound binding
against it, plus `datastream_projection`, `datamodel`,
`datastream_daily_breakdown_api`, `file_source_producer`. A route was written on
2026-08-01 to LIST it, with its own reason recorded: "nothing listed it [...]
there was no way, anywhere in the product, to find out which ids were". And the
only `INSERT INTO app.mdm_canonical_fields` in the repository are in five TEST
files. `datastream_field_mapping.py:740` says it plainly -- "registry is minted
ELSEWHERE". Elsewhere did not exist.

Consequence, measured 2026-08-08: 0 rows at both scopes, so the closed enumeration
a file-source Template validates against was closed on nothing, and
`FileSourceSamplePanel.tsx` offered an empty select with no explanation.

THE ARBITRATION THIS MODULE IMPLEMENTS. `file-source-ingestion.md:45-51` left it
open -- "either the Template grows an open metric set bounded by the declared
dimensions, or the extension point is a governed edit that mints a new Template
version [...] this is open, and it is the first thing to decide". Jean, 2026-08-08:
the two branches are the two SCOPES, not two products.

    project_id IS NULL      the platform vocabulary. Governed, not minted here.
    project_id = <project>  the client's own. They declare what they need.

The schema already encoded it -- two partial unique indexes, "the same name may
exist across scopes" (migration 032) -- and the mapping validator already accepts
both (`project_id IS NULL OR project_id = %s`). Nothing had to be decided about
the mechanism; only the door was absent.

WHAT DECIDED IT WAS THE USE CASE. On the derivation dossier a single video needs
`video_id, title, published_at, duration_s, category, views, restaurant, product,
recipe, film, confidence` -- ELEVEN fields, of which the thirteen governed ones
cover ZERO. Under a governed-edit-only extension point, describing one's own
workbook would take eleven publication ceremonies.

THIS DOOR MINTS PROJECT FIELDS ONLY. A platform field changes what every project
of the instance aligns on; minting one from a project-scoped door would let a
client edit the shared vocabulary. Refused here by name, rather than guarded by
whoever remembers.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from ulid import ULID

from core.audit import declare_action, insert_audit_row
from core.semantic_expressions import VALUE_TYPES

# --- LES ACTIONS QUE CE MODULE ECRIT ---------------------------------------
#
# LE MINT N ECRIVAIT AUCUN JOURNAL (mesure du 2026-08-16). Un champ canonique
# est la VOCABULAIRE contre lequel toute liaison mdm est validee : le minter, la
# retirer ou en changer la portee decide de ce que six modules de production
# acceptent. Rien n en gardait trace -- ni qui, ni quand, ni quoi. Une ligne
# apparaissait dans `app.mdm_canonical_fields` et le depot n avait aucune
# reponse a << d ou vient ce nom >>.
#
# L archivage compte AUTANT que le mint, et pour une raison qui lui est propre :
# il LIBERE le nom (l index unique partiel exclut les lignes archivees), donc
# deux champs successifs peuvent porter le meme nom sans que rien ne raconte le
# passage de l un a l autre.
#
# Declarees ICI parce que c est ici qu elles s ecrivent -- AD-42, meme regle que
# `business_taxonomy` : le module qui ecrit une action est celui qui la declare.
ACTION_CANONICAL_FIELD_DECLARED = declare_action("canonical_field.declared")
ACTION_CANONICAL_FIELD_ARCHIVED = declare_action("canonical_field.archived")

#: `app.mdm_canonical_fields.concept_kind` -- the CHECK of migration 032.
CONCEPT_KINDS: tuple[str, ...] = ("metric", "dimension")

#: The same CHECK's aggregation vocabulary. Not the semantic layer's
#: `AGGREGATION_FUNCTIONS` (which adds `count_distinct` and `median`): this column
#: has its own, narrower constraint, and widening it here would produce rows the
#: database refuses.
AGGREGATIONS: tuple[str, ...] = ("sum", "average", "min", "max", "count")

_NAME_MAX = 80

#: Le meme motif que `master_data_registries.object_kind` -- un champ qui nomme
#: un objet doit nommer un objet que le registre peut porter.
_OBJECT_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


class CanonicalFieldError(ValueError):
    """The declaration is refused, with the sentence that says where to repair."""


def _clean_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CanonicalFieldError("canonical_name is required")
    name = value.strip()
    if len(name) > _NAME_MAX:
        raise CanonicalFieldError(f"canonical_name is longer than {_NAME_MAX} characters")
    return name


def validate_declaration(
    *,
    canonical_name: Any,
    concept_kind: Any,
    value_type: Any,
    aggregation: Any = None,
    non_additive: Any = False,
    unit: Any = None,
    object_kind: Any = None,
) -> dict[str, Any]:
    """Return the normalized declaration, or refuse it with the reason.

    THE REFUSAL THAT MATTERS is the database's own invariant, applied here so the
    caller reads a sentence instead of a constraint name: a METRIC must either
    declare an aggregation OR be explicitly flagged non-additive
    (`ck_mdm_canonical_fields_metric_aggregation`). Its reason is written in
    migration 032 -- "a measure must never be silently non-summable" -- and it is
    the same rule the semantic compiler enforces one layer up. A metric that
    declared neither would be summed by whatever read it first.

    A dimension carries neither: an aggregation on a dimension is a column nobody
    reads, and `non_additive` on something that was never a measure says nothing.
    """
    name = _clean_name(canonical_name)

    if concept_kind not in CONCEPT_KINDS:
        raise CanonicalFieldError(
            f"concept_kind must be one of {', '.join(CONCEPT_KINDS)}, not {concept_kind!r}"
        )

    if value_type not in VALUE_TYPES:
        # Story 64.14. Cette moitie manquait, et son absence rendait le champ
        # impubliable en Concept : `semantic_concept_versions.value_type` est
        # NOT NULL. Le vocabulaire est celui de la couche semantique, importe et
        # jamais recopie -- une seconde liste aurait derive.
        raise CanonicalFieldError(
            f"value_type must be one of {sorted(VALUE_TYPES)}, not {value_type!r}"
        )

    if object_kind is not None:
        kind_text = str(object_kind).strip()
        if not _OBJECT_KIND_RE.match(kind_text):
            raise CanonicalFieldError(
                f"object_kind {object_kind!r} is not a usable kind name"
            )
        object_kind = kind_text

    non_additive = bool(non_additive)

    if concept_kind == "dimension":
        if aggregation is not None:
            raise CanonicalFieldError(
                "a dimension carries no aggregation -- it is not a measure"
            )
        if non_additive:
            raise CanonicalFieldError(
                "non_additive describes a measure; a dimension is never summed"
            )
        if unit is not None:
            raise CanonicalFieldError("a dimension carries no unit")
        return {
            "canonical_name": name,
            "concept_kind": "dimension",
            "value_type": value_type,
            "aggregation": None,
            "non_additive": False,
            "unit": None,
            "object_kind": object_kind,
        }

    if aggregation is not None and aggregation not in AGGREGATIONS:
        raise CanonicalFieldError(
            f"aggregation must be one of {', '.join(AGGREGATIONS)}, not {aggregation!r}"
        )
    if aggregation is None and not non_additive:
        raise CanonicalFieldError(
            f"the metric {name!r} declares neither an aggregation nor non_additive -- "
            "a measure must never be silently non-summable"
        )
    if aggregation is not None and non_additive:
        raise CanonicalFieldError(
            f"the metric {name!r} declares an aggregation AND non_additive -- "
            "the two contradict each other, and a reader would believe the first one found"
        )

    return {
        "canonical_name": name,
        "concept_kind": "metric",
        "value_type": value_type,
        "aggregation": aggregation,
        "non_additive": non_additive,
        "unit": (str(unit).strip() or None) if unit is not None else None,
        "object_kind": object_kind,
    }


#: The columns the read below returns, in the order the SELECT declares them.
_READ_COLUMNS: tuple[str, ...] = (
    "id",
    "project_id",
    "canonical_name",
    "concept_kind",
    "value_type",
    "aggregation",
    "non_additive",
    "unit",
    "object_kind",
    "description",
    "dictionary_field_name",
    "status",
)


def list_visible_canonical_fields(conn, *, project_id: str) -> list[dict[str, Any]]:
    """Every active canonical field this project can bind to, BOTH scopes.

    THE SAME PREDICATE THE VALIDATORS APPLY, and that is the whole point of
    reading it from one place: `file_source_template._assert_required_fields_
    registered` and `datastream_field_mapping` both accept
    `project_id IS NULL OR project_id = %s`, so a catalog narrower than that
    hides part of the vocabulary a binding would be accepted against, and a
    catalog wider than that offers a field the binding then refuses. Both are the
    same defect in opposite directions.

    `scope` IS DERIVED, NEVER STORED. `project_id IS NULL` is the platform
    vocabulary -- governed, and never minted from a project door
    (`declare_project_field` refuses it by name) -- and a row carrying the
    project id is the client's own. Storing the word beside the column that
    already decides it would give two answers to one question.

    Ordered platform first, then by kind and name, so the governed vocabulary a
    client aligns on is read before the vocabulary they added to it, and a
    dimension never interleaves with a metric in a picker.

    THE PARAMETER IS NAMED, and the rest of this module's are positional. It is
    not a style drift: this read is also a Governance lens
    (`governance_read_model._canonical_fields_lens`), and
    `test_every_query_is_scoped_by_the_authenticated_project_or_organization`
    asserts that every lens query names the scope it is bound to. A positional
    `%s` would pass the guard's eye while carrying the same scoping — and the
    guard exists precisely so nobody has to take that on trust.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, project_id, canonical_name, concept_kind, value_type,
                   aggregation, non_additive, unit, object_kind, description,
                   dictionary_field_name, status
            FROM app.mdm_canonical_fields
            WHERE status = 'active'
              AND (project_id = %(project_id)s OR project_id IS NULL)
            ORDER BY (project_id IS NOT NULL), concept_kind, canonical_name, id
            """,
            {"project_id": project_id},
        )
        rows = cur.fetchall()
    fields: list[dict[str, Any]] = []
    for row in rows:
        field = dict(zip(_READ_COLUMNS, row))
        field["scope"] = "platform" if field["project_id"] is None else "project"
        fields.append(field)
    return fields


def load_canonical_names(
    conn, *, project_id: str, field_ids: Sequence[str]
) -> dict[str, str]:
    """The human name of each canonical field: `{field_id: canonical_name}`.

    THE VOCABULARY IS RESOLVED WHERE IT LIVES. A canonical id is `mdm_<ULID>`
    (`declare_project_field` mints it), so any surface that prints the id where a
    person expects a name prints something nobody can act on. Resolving it in the
    browser instead would make the console a second authority on the vocabulary,
    and two authorities eventually disagree.

    `status` IS NOT FILTERED, and that is the difference with
    `list_visible_canonical_fields`. That read offers a catalog to bind against,
    so it must hide archived fields. This one names fields an IMMUTABLE artifact
    already pinned -- a compiled plan, a stored Query Spec version. Archiving a
    field does not rename what a plan compiled last month, and hiding the name
    would put the identifier back on the screen for the one case a person most
    needs the word.

    An id with no row is ABSENT from the result rather than mapped to a made-up
    name: the caller decides what an unnamed field shows, and this read never
    invents one.
    """
    return {
        field_id: identity["canonical_name"]
        for field_id, identity in load_canonical_field_identities(
            conn, project_id=project_id, field_ids=field_ids
        ).items()
        if identity["canonical_name"]
    }


def load_canonical_field_identities(
    conn, *, project_id: str, field_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """`{field_id: {"canonical_name", "status"}}`, with `status` UNFILTERED.

    THE STATUS IS READ, NOT INFERRED. A caller that holds the visible catalog
    (`list_visible_canonical_fields`, which is `status = 'active'`) can subtract
    one set from the other and conclude "archived" -- and that conclusion is only
    true while the two reads differ by nothing BUT status. It is one filter away
    from being quietly wrong, and the fact it stands for decides which gesture a
    refusal names: "declare it" and "restore it" are not interchangeable
    sentences. So the fact travels with the name, from the one row that holds it.

    `load_canonical_names` is the projection of this read that only wants a word.
    Same query, same scope, same absence rule: an id with no row is ABSENT from
    the result and is never mapped to an invented name.
    """
    wanted = [str(field_id) for field_id in field_ids if str(field_id or "").strip()]
    if not wanted:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, canonical_name, status
            FROM app.mdm_canonical_fields
            WHERE (project_id = %(project_id)s OR project_id IS NULL)
              AND id = ANY(%(field_ids)s)
            """,
            {"project_id": project_id, "field_ids": sorted(set(wanted))},
        )
        rows = cur.fetchall()
    return {
        str(row[0]): {
            "canonical_name": str(row[1]) if row[1] else "",
            "status": str(row[2] or ""),
        }
        for row in rows
        if row
    }


def declare_project_field(
    conn,
    *,
    project_id: str,
    canonical_name: str,
    concept_kind: str,
    value_type: str,
    actor: str,
    object_kind: str | None = None,
    aggregation: str | None = None,
    non_additive: bool = False,
    unit: str | None = None,
    description: str | None = None,
    dictionary_field_name: str | None = None,
) -> dict[str, Any]:
    """Mint one canonical field OWNED BY A PROJECT. Callers own the transaction.

    `project_id` is required and never optional: passing None would mint a
    PLATFORM field -- the vocabulary every project of the instance aligns on --
    from a door a client reaches. The refusal is the signature.

    `dictionary_field_name` optionally derives the field from the governed
    dictionary (`app.target_fields`, 13 rows), through the RESTRICT foreign key
    migration 032 already declared for exactly this: "a governed dictionary row
    cannot be dropped while a canonical id derives from it".
    """
    if not project_id or not str(project_id).strip():
        raise CanonicalFieldError(
            "a project is required -- this door mints project fields, never the "
            "platform vocabulary every project aligns on"
        )

    declaration = validate_declaration(
        canonical_name=canonical_name,
        concept_kind=concept_kind,
        value_type=value_type,
        aggregation=aggregation,
        non_additive=non_additive,
        unit=unit,
        object_kind=object_kind,
    )
    field_id = f"mdm_{ULID()}"

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.mdm_canonical_fields
                (id, project_id, concept_kind, canonical_name, value_type, object_kind,
                 unit, aggregation, non_additive, description, dictionary_field_name,
                 created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id, project_id, concept_kind, canonical_name, value_type,
                      object_kind, unit, aggregation, non_additive, description,
                      dictionary_field_name, status
            """,
            (
                field_id,
                str(project_id),
                declaration["concept_kind"],
                declaration["canonical_name"],
                declaration["value_type"],
                declaration["object_kind"],
                declaration["unit"],
                declaration["aggregation"],
                declaration["non_additive"],
                description,
                dictionary_field_name,
                actor,
            ),
        )
        row = cur.fetchone()

    if row is None:
        # The partial unique index on (project_id, canonical_name) WHERE status
        # != 'archived' refused it. Say the name back: "conflict" alone leaves the
        # caller guessing which of eleven declarations collided.
        raise CanonicalFieldError(
            f"{declaration['canonical_name']!r} is already an active canonical field "
            "in this project -- archive it before re-minting the name"
        )

    columns = (
        "id", "project_id", "concept_kind", "canonical_name", "value_type",
        "object_kind", "unit", "aggregation", "non_additive", "description",
        "dictionary_field_name", "status",
    )
    minted = dict(zip(columns, row))

    # DANS LA TRANSACTION DE L APPELANT, jamais a cote. `insert_audit_row` prend
    # la connexion pour cela : un champ minte sans trace, ou une trace sans
    # champ, sont deux etats faux -- et `declare_many` en compose onze d un coup.
    # `write_audit_row` aurait ouvert sa PROPRE connexion et commite seule.
    insert_audit_row(
        conn,
        identity=actor,
        action=ACTION_CANONICAL_FIELD_DECLARED,
        provider_account="governance",
        connection_ref="",
        metadata={
            "project_id": str(project_id),
            "scope": "project",
            "canonical_field_id": minted["id"],
            # Ce que le champ DECIDE, pas seulement qu il existe : la portee, la
            # sommabilite et l objet qualifie sont ce que les six lecteurs de
            # production appliquent ensuite.
            "canonical_name": minted["canonical_name"],
            "concept_kind": minted["concept_kind"],
            "value_type": minted["value_type"],
            "object_kind": minted["object_kind"],
            "aggregation": minted["aggregation"],
            "non_additive": minted["non_additive"],
            "dictionary_field_name": minted["dictionary_field_name"],
        },
    )
    return minted


def declare_many(
    conn, *, project_id: str, fields: Sequence[Mapping[str, Any]], actor: str
) -> list[dict[str, Any]]:
    """Mint several at once, ALL OR NOTHING within the caller's transaction.

    Eleven fields describe one video. Declaring them one call at a time would let
    a client stop halfway with a half-described object and a mapping that
    validates against some of its own columns -- which reads as "this file is
    partly wrong" rather than "I did not finish".
    """
    minted: list[dict[str, Any]] = []
    for index, entry in enumerate(fields):
        if not isinstance(entry, Mapping):
            raise CanonicalFieldError(f"fields[{index}] is not a declaration")
        minted.append(
            declare_project_field(
                conn,
                project_id=project_id,
                actor=actor,
                canonical_name=entry.get("canonical_name"),
                concept_kind=entry.get("concept_kind"),
                value_type=entry.get("value_type"),
                object_kind=entry.get("object_kind"),
                aggregation=entry.get("aggregation"),
                non_additive=entry.get("non_additive", False),
                unit=entry.get("unit"),
                description=entry.get("description"),
                dictionary_field_name=entry.get("dictionary_field_name"),
            )
        )
    return minted


def archive_project_field(
    conn, *, project_id: str, field_id: str, actor: str
) -> dict[str, Any]:
    """Retire a field. Nothing is deleted: a published mapping pins its id.

    Archiving is what frees the NAME -- the unique index excludes archived rows
    "so a name can be retired and re-minted" (migration 032).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.mdm_canonical_fields
               SET status = 'archived', updated_at = NOW()
             WHERE id = %s AND project_id = %s AND status = 'active'
            RETURNING id, project_id, canonical_name, status
            """,
            (field_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise CanonicalFieldError(
            "no active canonical field with that id in this project"
        )
    archived = dict(zip(("id", "project_id", "canonical_name", "status"), row))
    insert_audit_row(
        conn,
        identity=actor,
        action=ACTION_CANONICAL_FIELD_ARCHIVED,
        provider_account="governance",
        connection_ref="",
        metadata={
            "project_id": str(project_id),
            "scope": "project",
            "canonical_field_id": archived["id"],
            # Le nom est la raison d etre de cette ligne : l archivage le LIBERE,
            # donc c est le seul endroit ou l on peut lire qu il a change de main.
            "canonical_name": archived["canonical_name"],
            "released_name": archived["canonical_name"],
        },
    )
    return archived
