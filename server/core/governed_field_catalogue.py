"""The governed field catalogue, Semantic Model first (story 49.3, readers step).

WHY THIS MODULE EXISTS. `app.target_fields` is the legacy field dictionary. Its
five write doors were retired on 2026-08-25 (`governance.md`, amendment of that
day): every one of them answers 409 `legacy_store_is_read_only` and names the
Semantic Model as the gesture that works. The store therefore holds exactly what
migration 023 seeded and can never grow again -- while four consumers still asked
it, and only it, what the product's governed fields are. A person who declares a
metric in the Concept workbench today is invisible to all four.

THE ORDER, AND IT IS ONE-DIRECTIONAL. This module is the shape
`core.rollup.declared_non_additive_metrics` and `core.metric_semantics
.is_metric_monetary` already use: the Semantic Model answers FIRST, the legacy
dictionary is the fallback, and never the inverse. The successor may ADD a name
the dictionary never carried; it can never REMOVE one, because a name it does not
govern makes it ABSTAIN (the row is simply missing from its half) rather than
answer "no". Reading silence as a verdict is the failure `_semantic_model_declares
_money` names in its own docstring, and it is the failure this module inherits the
guard against.

WHICH SUCCESSOR ANSWERS WHAT, because there are two and the glossary keeps them
apart. A **Semantic Concept** (`app.semantic_concepts` + its immutable versions)
is a versioned definition carrying a value type, a label and an aggregation --
that is what a reader asking "what does the name `cost` MEAN" needs, and it is
what :func:`resolve` reads. A **Canonical Field** (`app.mdm_canonical_fields`) is
the vocabulary a BINDING is checked against -- it carries no formula and no
version, and it is what :func:`binding_vocabulary` reads. Merging them would make
every answer ambiguous the moment something tried to resolve it back.

SCOPE. A Project's own row wins over the platform row of the same name
(`ORDER BY project_id NULLS LAST`), the precedence every scoped read in this
repository already uses. `project_id=None` consults the PLATFORM half only: a
Concept belongs to a Project or to the platform catalogue, and borrowing one from
a scope the caller never named would serve a rule for nobody.

NO WRITES, and no connection of its own: the caller passes the connection that
already carries its access context, so a catalogue is never read from another
instant -- or another floor -- than the object it describes.
"""

from __future__ import annotations

from typing import Any

#: The ratified derivation table of `governance.md` § *The platform half is a
#: PROJECTION of the governed dictionary*, read in REVERSE. It states the two
#: vocabularies "differ by exactly that word": `target_fields.data_type` spells
#: `currency` where the Semantic Model spells `money`. Every other value type
#: keeps its name, so this map holds one entry and inventing more would be this
#: module deciding a vocabulary rather than reading one.
_DICTIONARY_TYPE_FOR_VALUE_TYPE = {"money": "currency"}

#: A `published` Concept version is the successor's approval. The dictionary
#: spells the same fact `approved`, and its readers branch on that token.
_PUBLISHED_READS_AS = "approved"

#: What a resolved field answers, in the dictionary's own column names. The
#: callers of this module were written against `app.target_fields`; translating
#: once here is what lets them move without four different rewrites.
COLUMNS = (
    "name",
    "display_name",
    "data_type",
    "field_kind",
    "measure",
    "description",
    "status",
)

_PUBLISHED_CONCEPTS = """
    SELECT c.name, c.project_id, v.kind, v.label, v.value_type, v.definition,
           v.aggregation, v.additivity_class
      FROM app.semantic_concepts c
      JOIN app.semantic_concept_versions v ON v.id = c.current_version_id
     WHERE c.lifecycle_status = 'published'
       AND v.status = 'published'
       AND (c.project_id IS NULL OR c.project_id = %(project_id)s)
       AND (%(all_names)s OR c.name = ANY(%(names)s))
     ORDER BY c.project_id NULLS LAST
"""

_DICTIONARY_ROWS = """
    SELECT name, display_name, data_type, field_kind, measure, description, status
      FROM app.target_fields
     WHERE status <> 'deleted'
       AND (NOT %(approved_only)s OR status = 'approved')
       AND (%(all_names)s OR name = ANY(%(names)s))
"""

_ACTIVE_CANONICAL_FIELDS = """
    SELECT canonical_name
      FROM app.mdm_canonical_fields
     WHERE status = 'active'
       AND (project_id IS NULL OR project_id = %(project_id)s)
"""


def _measure_of(aggregation: Any, additivity_class: Any) -> str | None:
    """The dictionary's `measure`, from the Concept's aggregation.

    The projection rule of `governance.md` runs the other way -- *"`aggregation`
    <- `target_fields.measure`, copied, unless the metric is non-additive"* --
    and this is the same sentence read back. A non-additive Concept answers NO
    measure rather than the function it happens to store: handing back `sum` for
    a metric its own version declares non-additive would be this module granting
    a permission to add, which is the direction the whole story forbids.
    """
    if additivity_class == "non_additive":
        return None
    if not isinstance(aggregation, dict):
        return None
    function = aggregation.get("function")
    return str(function) if function else None


def _concept_row_as_field(row: dict[str, Any]) -> dict[str, Any]:
    value_type = str(row.get("value_type") or "")
    return {
        "name": str(row["name"]),
        "display_name": row.get("label") or str(row["name"]),
        "data_type": _DICTIONARY_TYPE_FOR_VALUE_TYPE.get(value_type, value_type),
        "field_kind": row.get("kind"),
        "measure": _measure_of(row.get("aggregation"), row.get("additivity_class")),
        "description": row.get("definition"),
        "status": _PUBLISHED_READS_AS,
    }


def _fetch(conn: Any, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [description[0] for description in cur.description]
        return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]


def _name_params(names: list[str] | None) -> dict[str, Any]:
    """`all_names` is a BOOLEAN parameter, never a `%s IS NULL` guard (AI-186).

    A bare untyped parameter compared with `IS NULL` makes Postgres refuse the
    WHOLE statement, and `test_sql_parameter_typing` exists because that class
    came back eight times. A `bool` carries its own oid, so the same branch is
    expressed without one.
    """
    return {
        "all_names": names is None,
        "names": list(names or []),
    }


def resolve(
    conn: Any,
    *,
    names: list[str] | None = None,
    project_id: str | None = None,
    approved_only: bool = False,
) -> dict[str, dict[str, Any]]:
    """`{name -> governed field}` for *names*, Semantic Model first.

    ``names=None`` asks for the WHOLE catalogue. The answer is keyed by name and
    shaped in the dictionary's own columns (:data:`COLUMNS`), so a caller that
    used to read `app.target_fields` reads this instead without translating.

    ``approved_only`` keeps H1 -- *a draft field must not appear as an accepted
    target* -- for the callers that enforced it in their own WHERE clause. It
    never hides a Concept: only a PUBLISHED version is read at all, and published
    IS the successor's approval.

    A name no published Concept carries falls through to the dictionary; a name
    NEITHER store carries is absent from the result, and the caller keeps whatever
    it did with an unknown name before.
    """
    resolved: dict[str, dict[str, Any]] = {}
    params = {
        **_name_params(names),
        "project_id": project_id,
        "approved_only": approved_only,
    }
    for row in _fetch(conn, _PUBLISHED_CONCEPTS, params):
        # `ORDER BY project_id NULLS LAST` puts the Project's own row BEFORE the
        # platform row of the same name, and the first one seen is kept -- the
        # exact semantics of `_semantic_model_declares_money`'s `LIMIT 1`, so the
        # two readers cannot disagree about which scope has the last word.
        resolved.setdefault(str(row["name"]), _concept_row_as_field(row))
    for row in _fetch(conn, _DICTIONARY_ROWS, params):
        name = str(row["name"])
        if name in resolved:
            continue
        resolved[name] = {column: row.get(column) for column in COLUMNS}
    return resolved


def governed_names(
    conn: Any,
    *,
    names: list[str],
    project_id: str | None = None,
) -> set[str]:
    """Which of *names* the product governs -- under either store.

    The validator's half of :func:`resolve`. A name is governed when a published
    Concept carries it OR the dictionary still does; the caller refuses the rest.
    """
    if not names:
        return set()
    return set(resolve(conn, names=list(names), project_id=project_id))


def binding_vocabulary(conn: Any, *, project_id: str | None = None) -> set[str]:
    """The canonical names a BINDING may claim, Canonical Fields first.

    NOT :func:`resolve`, and the difference is the glossary's. § *The rule this
    settles*: *"a binding names a canonical field id, never a Concept"*, and
    `app.mdm_canonical_fields` is described there as *"the vocabulary a binding is
    checked against"*. So the successor for THIS question is the registry, and
    reading Concepts here would tell a person their column is bound to an object
    that, in the declaration panel's own words, *binds no column*.

    `app.target_fields` remains the layer below, for the same one-directional
    reason as everywhere else in this module: the registry may add names the
    dictionary never carried -- measured 2026-08-15, 286 distinct canonical
    targets are declared by the manifests in this repository against the
    dictionary's 15 -- and it may never take one away. Only APPROVED dictionary
    rows join, which is the H1 rule the caller used to spell in its own WHERE.
    """
    names = {
        str(row["canonical_name"])
        for row in _fetch(conn, _ACTIVE_CANONICAL_FIELDS, {"project_id": project_id})
    }
    for row in _fetch(
        conn,
        _DICTIONARY_ROWS,
        {**_name_params(None), "approved_only": True},
    ):
        names.add(str(row["name"]))
    return names
