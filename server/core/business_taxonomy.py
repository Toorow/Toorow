"""Organization-owned business taxonomy for the Context Hub (Story 45.1).

READ-ONLY FOR IDENTITIES SINCE 2026-08-25 -- story 49.3, the acceptance schedule
Jean ratified in `docs/product-architecture/governance.md`: *"The legacy writers
refuse from now on (409 naming the convergence gesture); an unconverged
organization converges first, then writes through the Master Data authority. No
window in which two authorities write for the same org."*

WHAT REFUSES, AND WHAT DOES NOT. The four writers that MINT OR EDIT A BUSINESS
IDENTITY -- :func:`create_domain`, :func:`update_domain`,
:func:`create_classification`, :func:`update_classification` -- raise
:class:`LegacyTaxonomyWriteRefused` before touching anything. Those are exactly
the two kinds `core.master_data_convergence` moves into the authority, keeping
each row's own id.

Everything else stays, and each for a stated reason:

* every READ. `list_taxonomy`, `get_domain`, `graph_projection`,
  `resolve_business_path` and the rest are what the Context Hub, the Datastream
  workbench and the evaluation corpus resolve a business route with. A superseded
  source is *"readable forever"*, which is the other half of the same sentence;
* :func:`retire_link` -- the governed withdrawal of 2026-08-25. It destroys
  nothing and it is not an identity;
* :func:`create_link`. A business link is NOT converged: the amendment says so in
  its own words -- *"It does not move `app.mdm_business_links`. A link is a
  Context Hub relation between a governed object and a consumer, not an
  identity"* -- so there is no Master Data gesture to name in a refusal. Cutting
  it would also break the amendment's own *Incomplete if*: *"a withdrawn link
  cannot be made again, so one wrong click has no repair"*. A retirement whose
  repair does not exist is a deletion with extra steps.

Postgres is the sole writer of what remains. Callers own the transaction so each
mutation and its audit evidence commit atomically. The six starter domains live
in migration seed data; this module never treats them as an enum.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

import psycopg
from ulid import ULID

from core import business_identity_catalogue as catalogue
from core.audit import declare_action, insert_audit_row

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12). Elles passaient par le wrapper local `_audit`, donc
# le releve des appelants directs de `write_audit_row` ne les voyait pas :
# un ecrivain INDIRECT est un ecrivain. Le garde de conformance suit
# maintenant les wrappers qui transmettent `action`.
# UNE ACTION COMPOSEE A L EXECUTION SE DECLARE TERME PAR TERME. Deux ecritures
# d ici composent `f"business_domain.{change_kind}"` ; le refus d AD-42 les a
# mises au jour, parce qu une chaine assemblee au vol n est verifiable par rien.
# `change_kind` vaut created | updated | archived | restored -- alors la
# famille se nomme, et une cinquieme valeur ajoutee un jour echoue ICI plutot
# que d atterrir muette dans le journal.
_DOMAIN_CHANGE_KINDS = ("created", "updated", "archived", "restored")
BUSINESS_DOMAIN_ACTIONS = {
    kind: declare_action(f"business_domain.{kind}") for kind in _DOMAIN_CHANGE_KINDS
}
BUSINESS_CLASSIFICATION_ACTIONS = {
    kind: declare_action(f"business_classification.{kind}") for kind in _DOMAIN_CHANGE_KINDS
}
ACTION_BUSINESS_CLASSIFICATION_CREATED = BUSINESS_CLASSIFICATION_ACTIONS["created"]
ACTION_BUSINESS_DOMAIN_CREATED = BUSINESS_DOMAIN_ACTIONS["created"]
# `business_link.deleted` was the name while the row was destroyed. It is gone
# rather than kept beside its successor: an action code names what HAPPENED, and
# two codes for one gesture make the audit log answer "was this link deleted or
# withdrawn?" with both. Rows already written under the old code keep it -- the
# log is append-only and that is exactly what makes it evidence.
ACTION_BUSINESS_LINK_RETIRED = declare_action("business_link.retired")


# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12). Celles-ci n'etaient declarees NULLE PART : la valeur
# etait retapee en dur ici, parce que la liste centrale de `core/audit.py`
# etait trop loin pour valoir le detour. Mesure ce jour-la sur le journal
# vivant : 29 des 64 actions reellement ecrites -- 45 % -- etaient dans ce
# cas, et rien ne pouvait distinguer une action d'une faute de frappe.
ACTION_BUSINESS_LINK_CREATED = declare_action("business_link.created")



class BusinessTaxonomyError(ValueError):
    """An unprocessable business taxonomy action was requested."""


class TaxonomyNotFoundError(BusinessTaxonomyError):
    """The requested domain, classification or link does not exist."""


class StaleTaxonomyVersionError(BusinessTaxonomyError):
    """The taxonomy item version does not match the expected version."""


class DuplicateTaxonomySlugError(BusinessTaxonomyError):
    """A domain or classification slug is already in use within the org."""


class TaxonomyCycleError(BusinessTaxonomyError):
    """Raised when a hierarchy edit would introduce a cycle."""


class LegacyTaxonomyWriteRefused(Exception):
    """A superseded identity writer was called. The caller sees 409, never 404.

    NOT a `BusinessTaxonomyError`, and that is the whole point of the class. Four
    product paths catch `BusinessTaxonomyError` to keep going when a taxonomy act
    is merely impossible -- `context_seed._ensure_domain_link` logs and returns,
    for instance. A cutover swallowed by an existing `except` is a cutover nobody
    can see: it would read as "nothing to write" and the store would quietly stay
    the authority in the one place it was supposed to stop being one.
    """


#: One code and one sentence for the four doors, on the shape story 49.3
#: established for `app.metric_definitions` and `app.target_fields` on the same
#: day: a refusal, never an unmounted route. Unmounting answers 404, which tells
#: a caller its object does not exist and sends it looking for it.
LEGACY_TAXONOMY_WRITE_REFUSED_CODE = "legacy_store_is_read_only"

#: It names the two gestures in the order they have to happen, and no store,
#: column or deployment state -- `CLAUDE.md`, *"Un message d'erreur nomme le
#: geste qui repare"*.
LEGACY_TAXONOMY_WRITE_REFUSED_MESSAGE = (
    "This business taxonomy no longer takes writes. Converge this organization "
    "into Master Data first -- one governed command on the Governance Master "
    "Data screen, which keeps every Business Domain and classification at the "
    "identity it already has -- then create, rename or archive it there, where "
    "every screen reads it from."
)


def _refuse_legacy_taxonomy_write() -> None:
    """The single throw site. One sentence, identical for every caller."""
    raise LegacyTaxonomyWriteRefused(LEGACY_TAXONOMY_WRITE_REFUSED_MESSAGE)


#: The columns a taxonomy row answers with. They are the CATALOGUE's, not this
#: module's own copy of the superseded store's DDL: since story 49.2 every read
#: here resolves through `core.business_identity_catalogue`, which answers from
#: `app.master_data_nodes` first and carries a `source` column saying which store
#: replied. Re-declaring the list here would let the two drift by one column and
#: make `zip(..., strict=False)` silently shift every field after it.
_DOMAIN_COLUMNS = catalogue.DOMAIN_COLUMNS
_CLASSIFICATION_COLUMNS = catalogue.CLASSIFICATION_COLUMNS


def _mint(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _required(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BusinessTaxonomyError(f"{label} is required")
    return value.strip()


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BusinessTaxonomyError("optional text values must be strings")
    return value.strip() or None


def normalize_slug(value: str, label: str = "slug") -> str:
    """Return a stable client-defined slug without constraining its vocabulary.

    *label* names the FIELD being normalized, and every refusal is phrased with
    it. Three call sites normalize something that is not the slug --
    `classification_type` and `relation_type` -- and the message was hard-coded
    to "slug", so a console that omitted `classification_type` was told "slug is
    required" and the operator went looking at the wrong field (live finding
    F3). A validation message that names another field is worse than none.
    """
    raw = _required(value, label)
    ascii_value = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")
    if not slug:
        raise BusinessTaxonomyError(f"{label} must contain letters or digits")
    if len(slug) > 96:
        raise BusinessTaxonomyError(f"{label} must be 96 characters or fewer")
    return slug


#: Canonical relation vocabulary for governed business links, AFTER
#: normalize_slug (so the console's "applies_to" arrives as "applies-to").
#: Mirrors the Link dialog options (ui/admin/src/connaissances/ContextHubLayout.tsx);
#: free-form strings made every link its own vocabulary (live finding F2).
ALLOWED_RELATION_TYPES = frozenset({"explains", "owns", "applies-to", "uses"})


def ensure_no_cycle(
    classification_id: str,
    parent_id: str | None,
    descendant_ids: set[str],
) -> None:
    if parent_id is None:
        return
    if parent_id == classification_id or parent_id in descendant_ids:
        raise TaxonomyCycleError("Business classification hierarchy cannot contain a cycle")


def _dict_from_row(row: Any, columns: Iterable[str]) -> dict[str, Any]:
    if row is None:
        raise TaxonomyNotFoundError("Taxonomy node not found")
    return dict(zip(columns, row, strict=False))


def _audit(
    conn,
    *,
    actor: str,
    action: str,
    org_id: str,
    resource_id: str,
    reason: str,
    trace_id: str | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    insert_audit_row(
        conn,
        identity=_required(actor, "actor"),
        action=action,
        provider_account="context-hub",
        connection_ref="",
        metadata={
            "effective_org_id": org_id,
            "resource_id": resource_id,
            "reason": _required(reason, "reason"),
            "trace_id": trace_id,
            "before": before,
            "after": after,
        },
    )


def list_taxonomy(
    conn, *, org_id: str, status: str = "active", limit: int | None = None
) -> dict[str, Any]:
    """The organization's Business Domains and classifications, with their base.

    `current_version_id` is the authority's CURRENT published revision of each
    identity, or None where it has none -- the precondition a rename door has to
    send back (`governance.md`, amendment of 2026-08-30). It sits beside
    `version_number` and does not replace it: the number is what a reader is
    SHOWN, the id is what a writer is HELD TO, and they cannot be the same value
    because the number is minted above every ledger's maximum since 2026-08-31
    (one number, one content; see master_data._NEXT_NODE_VERSION_NUMBER).
    """

    org_id = _required(org_id, "org_id")
    # Strict lifecycle filter, same contract as the topics/procedures lists:
    # "active" (default), "archived" or "all" -- `status=archived` must return
    # the archived rows, not nothing, and `status=all` both kinds (finding F1).
    if status == "active":
        status_clause = "AND status = 'active'"
    elif status == "archived":
        status_clause = "AND status = 'archived'"
    elif status == "all":
        status_clause = ""
    else:
        raise BusinessTaxonomyError(
            "status must be one of: active, archived, all"
        )
    limit_sql = ""
    params: list[Any] = [org_id]
    if limit is not None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > 1000
        ):
            raise BusinessTaxonomyError("limit must be between 1 and 1000")
        limit_sql = " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_DOMAIN_COLUMNS)},
                   COALESCE((SELECT max(v.version_number)
                             FROM {catalogue.DOMAIN_VERSION_SOURCE} v
                             WHERE v.domain_id = d.id AND v.org_id = d.org_id), 1)
                     AS version_number,
                   {catalogue.CURRENT_VERSION_ID.format(
                       id_expression="d.id", org_expression="d.org_id"
                   )} AS current_version_id
            FROM {catalogue.DOMAIN_SOURCE} d
            WHERE org_id = %s {status_clause}
            ORDER BY name, id
            {limit_sql}
            """,
            params,
        )
        domains = [
            dict(
                zip(
                    (*_DOMAIN_COLUMNS, "version_number", "current_version_id"),
                    row,
                    strict=False,
                )
            )
            for row in cur.fetchall()
        ]
        if limit is not None:
            classifications = []
        else:
            cur.execute(
                f"""
                SELECT {", ".join(_CLASSIFICATION_COLUMNS)},
                       COALESCE((SELECT max(v.version_number)
                                 FROM {catalogue.CLASSIFICATION_VERSION_SOURCE} v
                                 WHERE v.classification_id = c.id
                                   AND v.org_id = c.org_id), 1) AS version_number,
                       {catalogue.CURRENT_VERSION_ID.format(
                           id_expression="c.id", org_expression="c.org_id"
                       )} AS current_version_id
                FROM {catalogue.CLASSIFICATION_SOURCE} c
                WHERE org_id = %s {status_clause}
                ORDER BY domain_id, parent_id NULLS FIRST, name, id
                """,
                (org_id,),
            )
            classifications = [
                dict(
                    zip(
                        (*_CLASSIFICATION_COLUMNS, "version_number", "current_version_id"),
                        row,
                        strict=False,
                    )
                )
                for row in cur.fetchall()
            ]
    return {"org_id": org_id, "domains": domains, "classifications": classifications}


def list_domain_picker(
    conn, *, org_id: str, status: str = "active", limit: int = 201
) -> list[dict[str, Any]]:
    """Read compact immutable Domain identities without loading descriptive text."""

    org_id = _required(org_id, "org_id")
    if status == "active":
        status_clause = "AND d.status = 'active'"
    elif status == "archived":
        status_clause = "AND d.status = 'archived'"
    elif status == "all":
        status_clause = ""
    else:
        raise BusinessTaxonomyError("status must be one of: active, archived, all")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 1000:
        raise BusinessTaxonomyError("limit must be between 1 and 1000")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT d.id, left(d.name, 160) AS name,
                   COALESCE((SELECT max(v.version_number)
                               FROM {catalogue.DOMAIN_VERSION_SOURCE} v
                              WHERE v.domain_id = d.id AND v.org_id = d.org_id), 1)
                     AS version_number
              FROM {catalogue.DOMAIN_SOURCE} d
             WHERE d.org_id = %s {status_clause}
             ORDER BY d.name, d.id
             LIMIT %s
            """,
            (org_id, limit),
        )
        return [
            {"id": str(row[0]), "name": str(row[1]), "version_number": int(row[2])}
            for row in cur.fetchall()
        ]


def get_domain(conn, *, org_id: str, domain_id: str) -> dict[str, Any]:
    """One Business Domain, from the authority first (story 49.2).

    THE `for_update` SEAM WENT WITH THE WRITERS. It existed so `update_domain`
    could lock the row it was about to version, and those four doors answer 409
    since the cutover -- measured on the way in, no caller in this repository
    passed it any more. A row lock on a UNION of two stores is not expressible
    anyway, and keeping a parameter that can only raise would be a trap for the
    next writer rather than a service to it.
    """
    identity = catalogue.resolve_domain(
        conn,
        org_id=_required(org_id, "org_id"),
        domain_id=_required(domain_id, "domain_id"),
    )
    if identity is None:
        raise TaxonomyNotFoundError("Taxonomy node not found")
    return identity


def get_classification(conn, *, org_id: str, classification_id: str) -> dict[str, Any]:
    """One Business Classification, from the authority first (story 49.2)."""
    identity = catalogue.resolve_classification(
        conn,
        org_id=_required(org_id, "org_id"),
        classification_id=_required(classification_id, "classification_id"),
    )
    if identity is None:
        raise TaxonomyNotFoundError("Taxonomy node not found")
    return identity


def create_domain(
    conn,
    *,
    org_id: str,
    name: str,
    slug: str,
    description: str = "",
    owner: str | None = None,
    actor: str,
    reason: str,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """REFUSED since 2026-08-25 -- a Business Domain is minted in Master Data.

    WHAT WENT WITH IT. The INSERT into the taxonomy store and the version row
    beside it: `_append_domain_version` had no other caller once the four writers
    of this module stopped writing, and a version-appending helper that nobody
    appends with is the exact shape a re-wiring grows back through.

    THE `domain_id` SEAM WENT TOO. It existed for one caller -- the evaluation
    seeder, which has to NAME the domain its offline corpus expects rather than
    receive a minted id -- and it existed so that seeder would not grow a second
    copy of this INSERT. There is no INSERT left to share, so the seam has
    nothing to offer; `server/tests/evals/seed_eval_platform.py` writes its own
    fixture row into a disposable `_test` database, which is what it already does
    for the membership and the grant it needs.

    Raises:
        LegacyTaxonomyWriteRefused: always.
    """
    _refuse_legacy_taxonomy_write()


#: Une version de Semantic Model dont les references tiennent encore. Un `draft`
#: ne donne permission a rien aujourd hui, mais quelqu un peut le publier demain.
#: `superseded` et `archived` sont de l HISTOIRE : elles ne reviennent jamais, et
#: bloquer dessus rendrait un domaine inarchivable a vie.
LIVE_VERSION_STATUSES = ("draft", "candidate", "published")


def domain_used_by(conn, *, org_id: str, domain_id: str) -> list[dict[str, Any]]:
    """Les versions du Modele Semantique qui referencent ce Business Domain.

    POURQUOI CE LECTEUR EXISTE (mesure du 2026-08-16). Le sens ENTRANT etait
    garde : `semantic_model._validate_business_domain_refs` refuse par son nom une
    nouvelle version qui lie un domaine archive, et sa raison est la bonne -- une
    version publiee est IMMUABLE, donc une reference qui ne resout pas ne peut
    jamais etre corrigee en place. Le sens SORTANT ne l etait pas : archiver un
    domaine que des versions publiees referencent deja passait en silence, et
    laissait derriere exactement ces references-la, incorrigibles.

    Rend TOUTES les references, histoire comprise, chacune disant le cycle de vie
    de la version ou elle vit. Filtrer ici rendrait impossible de distinguer << ce
    qui tient >> de << toute l histoire >>.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, 'semantic-view' AS kind, name, status
              FROM app.semantic_view_versions
             WHERE business_domain_refs ? %(domain_id)s
               AND project_id IN (SELECT id FROM app.projects WHERE org_id = %(org_id)s)
            UNION ALL
            SELECT id, 'semantic-concept' AS kind, name, status
              FROM app.semantic_concept_versions
             WHERE business_domain_refs ? %(domain_id)s
               AND project_id IN (SELECT id FROM app.projects WHERE org_id = %(org_id)s)
            ORDER BY 2, 3
            """,
            {"domain_id": domain_id, "org_id": org_id},
        )
        rows = cur.fetchall()
    return [
        {
            "version_id": row[0],
            "object_type": row[1],
            "name": row[2],
            "status": row[3],
            "still_holds": str(row[3]) in LIVE_VERSION_STATUSES,
        }
        for row in rows
    ]


def update_domain(
    conn,
    *,
    org_id: str,
    domain_id: str,
    patch: dict[str, Any],
    actor: str,
    reason: str,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """REFUSED since 2026-08-25 -- renaming or archiving is a Master Data act.

    ONE GUARD LEFT THIS MODULE WITH IT, AND IT IS NAMED RATHER THAN MOURNED.
    Archiving used to be refused while a live Semantic Model version still
    referenced the domain (`domain_used_by`, 2026-08-16): a published version is
    immutable, so a reference that stops resolving can never be corrected in
    place. Master Data's own archive is impact-guarded too
    (`master_data_commands.run_node_command` -> `assess_node_impact`), but it
    read `app.master_data_used_by` and nothing registered a Semantic Model
    version's Business Domain reference there.

    THAT HOLE CLOSED THE SAME DAY, AND THE WRITER IS THE PUBLISHER.
    `core.semantic_model_used_by.register_published_version` is called by
    `semantic_model._apply_concept` / `_apply_view` inside the confirm
    transaction; the supersession releases the version it replaced;
    `_apply_archive` releases a retired object's rows; and the convergence
    registers what was published before the identity was governed. On the
    authority's side the guard is `master_data.assess_org_node_impact` /
    `archive_org_node_guarded` -- organization-scoped, because a converged
    Business Domain is an organization node and the Project-scoped read cannot
    ask the question at all.

    `domain_used_by` stays, and its role changed: it was kept as "the reading
    that guard needs", and the guard now has its own. It remains the reading that
    answers over ALL of history, live versions and superseded ones alike, which
    the used-by store deliberately does not -- the store carries what still
    holds.

    Raises:
        LegacyTaxonomyWriteRefused: always.
    """
    _refuse_legacy_taxonomy_write()


def create_classification(
    conn,
    *,
    org_id: str,
    domain_id: str,
    parent_id: str | None,
    classification_type: str,
    slug: str,
    name: str,
    description: str = "",
    owner: str | None = None,
    actor: str,
    reason: str,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """REFUSED since 2026-08-25 -- a classification is minted in Master Data.

    `classification_type` is not lost by the cutover and it is not interpreted
    either: the convergence CARRIES the word the organization chose into the
    version payload, and the type registry it publishes declares it a free
    string. Deciding that a given classification is really a Product stays a
    human act, taken later, one row at a time.

    `_validate_parent` and `_descendant_ids` went with this writer and with
    `update_classification`. The hierarchy invariants they enforced in Python are
    enforced by the database on both sides: migration 130's
    `trg_mdm_business_classifications_no_cycle` on this store, and
    `master_data.replace_org_draft_memberships` on the authority.

    Raises:
        LegacyTaxonomyWriteRefused: always.
    """
    _refuse_legacy_taxonomy_write()


def update_classification(
    conn,
    *,
    org_id: str,
    classification_id: str,
    patch: dict[str, Any],
    actor: str,
    reason: str,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """REFUSED since 2026-08-25 -- editing a classification is a Master Data act.

    Moving a classification under another parent was this function's real work,
    and the authority does it as a change to the object that moved: one regroup,
    one version, on the child. That is `replace_org_draft_memberships`, published
    like any other version -- an append rather than an in-place rewrite.

    Raises:
        LegacyTaxonomyWriteRefused: always.
    """
    _refuse_legacy_taxonomy_write()


def serialize_snapshot(value: dict[str, Any]) -> str:
    """Stable JSON helper shared by path-key hashing in the next task."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


BUSINESS_TAXONOMY_TYPES = frozenset({"business_domain", "business_classification"})
#: What a governed business key may be attached to.
#:
#: `context-hub.md` refuses the story as incomplete while "a view, Datastream or
#: semantic object cannot be linked to business context". Two of those three were
#: here already -- `report_view` and `datastream`. `semantic_view` and
#: `semantic_concept` were in NEITHER this set nor the browser's union, so a
#: published Semantic View, the object Governance treats as the business meaning
#: of a metric, could not be attached to the Business Domain it belongs to.
#:
#: Added as two distinct types on purpose. A View and a Concept are different
#: governed objects with different owners and different versions; collapsing them
#: into one "semantic" target would make the link ambiguous the moment anything
#: tried to resolve it back.
#:
#: `canonical_field` is the same shape of hole, one layer down (AI-298). It is
#: NOT a duplicate of `target_field`: that one names a row of the governed
#: dictionary `app.target_fields` BY NAME, and the dictionary is one bounded
#: platform list. A field a project declared under its own source lives in
#: `app.mdm_canonical_fields`, has an id and no dictionary row -- so Governance
#: listed it and a Datastream mapping bound it (`binding.mdm_target` -> that id)
#: while Context Hub could name nothing but the dictionary. `governance.md`
#: refuses the surface as incomplete while "a Datastream mapping cannot bind to
#: the same object Governance and Context Hub reference"; the two field types are
#: two vocabularies with two resolution keys, and merging them would have made
#: the link ambiguous exactly as collapsing the two semantic types would.
BUSINESS_TARGET_TYPES = frozenset(
    {
        "topic",
        "procedure",
        "target_field",
        "canonical_field",
        "schema_doc",
        "report_view",
        "datastream",
        "semantic_view",
        "semantic_concept",
    }
)
_LINK_COLUMNS = (
    "id",
    "org_id",
    "project_id",
    "taxonomy_type",
    "taxonomy_id",
    "target_type",
    "target_id",
    "relation_type",
    "link_origin",
    "created_by",
    "created_at",
    # Migration 306. Read everywhere the link is: a caller that receives a link
    # without its retirement cannot tell a live one from a withdrawn one, and
    # that is the distinction the column exists to keep.
    "retired_at",
    "retired_by",
    "retired_reason",
)


def _taxonomy_names(conn, *, org_id: str, links: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """`{taxonomy_id: name}` for the taxonomy nodes a list of links points at.

    WHY THE READ IS HERE AND NOT IN THE BROWSER. A link row carries the id of the
    Business Domain or classification it hangs off and no word for it, so every
    console reading links had to resolve the word itself -- the Knowledge Library
    and the Skill editor both did, with
    ``domains.find((d) => d.id === link.taxonomy_id)?.name ?? link.taxonomy_id``,
    and printed ``bdom_<ULID>`` on a pill whenever the catalogue they happened to
    have loaded did not carry that node. That is the console standing up as a
    second authority on the vocabulary, which
    ``docs/product-architecture/visualization-and-rendering.md`` refuses ("The
    name is resolved on the server, where the vocabulary lives"; incomplete "if a
    label is composed in the browser").

    Both taxonomy kinds are read in ONE statement each, never per link, and both
    read the same UNION the catalogue serves everywhere else -- so a node held by
    the authority store and a node still on the superseded row answer with the
    same word. An id that resolves to nothing is simply absent from the map: the
    caller then serves ``taxonomy_name: None``, and the reader is told the
    taxonomy is no longer readable rather than handed its address.
    """

    wanted: dict[str, set[str]] = {"business_domain": set(), "business_classification": set()}
    for link in links:
        kind = str(link.get("taxonomy_type") or "")
        node = str(link.get("taxonomy_id") or "")
        if node and kind in wanted:
            wanted[kind].add(node)
    names: dict[str, str] = {}
    sources = (
        ("business_domain", catalogue.DOMAIN_SOURCE),
        ("business_classification", catalogue.CLASSIFICATION_SOURCE),
    )
    with conn.cursor() as cur:
        for kind, source in sources:
            if not wanted[kind]:
                continue
            cur.execute(
                f"SELECT id, name FROM {source} AS t "  # noqa: S608 -- both sources are module constants
                "WHERE t.org_id = %s AND t.id = ANY(%s)",
                (org_id, sorted(wanted[kind])),
            )
            for node_id, name in cur.fetchall():
                if name:
                    names[str(node_id)] = str(name)
    return names


def _validate_link_kinds(taxonomy_type: str, target_type: str) -> None:
    if taxonomy_type not in BUSINESS_TAXONOMY_TYPES:
        raise BusinessTaxonomyError(
            "taxonomy_type must be business_domain or business_classification"
        )
    if target_type not in BUSINESS_TARGET_TYPES:
        # Derived from the set rather than retyped beside it. The hand-written
        # list was accurate until this commit added two types to the set above --
        # exactly the drift that makes a refusal name fewer options than the
        # caller actually has.
        raise BusinessTaxonomyError(
            "target_type must be one of " + ", ".join(sorted(BUSINESS_TARGET_TYPES))
        )


def _assert_project_org(conn, *, project_id: str, org_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM app.projects WHERE id = %s AND org_id = %s AND status = 'active'",
            (project_id, org_id),
        )
        if cur.fetchone() is None:
            raise TaxonomyNotFoundError("Project not found")


def _assert_taxonomy_source(
    conn, *, org_id: str, taxonomy_type: str, taxonomy_id: str
) -> dict[str, Any]:
    if taxonomy_type == "business_domain":
        row = get_domain(conn, org_id=org_id, domain_id=taxonomy_id)
    else:
        row = get_classification(conn, org_id=org_id, classification_id=taxonomy_id)
    if row["status"] != "active":
        raise BusinessTaxonomyError("Archived taxonomy nodes cannot receive links")
    return row


def validate_target_reference(
    conn,
    *,
    project_id: str,
    target_type: str,
    target_id: str,
    loaded_modules=None,
) -> None:
    """Validate a target through its existing project-scoped source of truth."""
    if target_type not in BUSINESS_TARGET_TYPES:
        raise BusinessTaxonomyError("Unknown governed target type")
    target_id = _required(target_id, "target_id")
    if target_type == "report_view":
        if target_id.count("/") != 1:
            raise BusinessTaxonomyError("report_view target_id must be <module>/<report_id>")
        module_name, report_id = target_id.split("/", 1)
        from core.report_chain import get_report_chain  # noqa: PLC0415

        chain = get_report_chain(
            project_id,
            module_name,
            report_id,
            conn,
            loaded_modules=loaded_modules,
        )
        if chain is None:
            raise TaxonomyNotFoundError("Governed target not found")
        return

    query_by_type = {
        "topic": (
            "SELECT 1 FROM app.context_topics WHERE id = %s AND status = 'active' "
            "AND (project_id IS NULL OR project_id = %s)",
            (target_id, project_id),
        ),
        "procedure": (
            "SELECT 1 FROM app.procedures WHERE id = %s AND status = 'active' "
            "AND (project_id IS NULL OR project_id = %s)",
            (target_id, project_id),
        ),
        "target_field": (
            "SELECT 1 FROM app.target_fields WHERE name = %s AND status <> 'deleted'",
            (target_id,),
        ),
        # Both scopes, and the id is the key. `IS NULL OR =` is the scope rule of
        # this repository: a platform field is visible to every project, and a
        # project field only to its own. Resolving this one by NAME instead would
        # have recreated the hole it closes -- two projects may each declare a
        # field called `revenue_net`, and only the id tells them apart.
        "canonical_field": (
            "SELECT 1 FROM app.mdm_canonical_fields WHERE id = %s "
            "AND (project_id IS NULL OR project_id = %s) AND status = 'active'",
            (target_id, project_id),
        ),
        "schema_doc": (
            "SELECT 1 FROM app.schema_context WHERE id = %s AND project_id = %s",
            (target_id, project_id),
        ),
        "datastream": (
            "SELECT 1 FROM app.datastreams WHERE id = %s AND project_id = %s",
            (target_id, project_id),
        ),
        # Both are validated against the OBJECT, never a version: a business link
        # says "this concept belongs to this domain", which stays true across
        # versions. Pinning a version here would break the link every time the
        # owner published, and re-pointing it would be Context Hub editing
        # Governance's state.
        "semantic_view": (
            "SELECT 1 FROM app.semantic_views WHERE id = %s AND project_id = %s",
            (target_id, project_id),
        ),
        "semantic_concept": (
            "SELECT 1 FROM app.semantic_concepts WHERE id = %s AND project_id = %s",
            (target_id, project_id),
        ),
    }
    sql, params = query_by_type[target_type]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.fetchone() is None:
            raise TaxonomyNotFoundError("Governed target not found")


def create_link(
    conn,
    *,
    org_id: str,
    project_id: str,
    taxonomy_type: str,
    taxonomy_id: str,
    target_type: str,
    target_id: str,
    relation_type: str,
    actor: str,
    reason: str,
    trace_id: str | None = None,
    loaded_modules=None,
    link_origin: str = "direct",
) -> dict[str, Any]:
    """Poser un lien gouverne. `link_origin` dit QUI l'a decide.

    `direct` : quelqu'un a decide. `derived` : une machine a propose et un humain
    a confirme. Sans cette distinction -- et elle n'existait pas avant la
    migration 202 -- un agent qui comble un lien manquant produit un lien
    indiscernable d'une decision humaine, et l'arbre cesse d'etre gouverne.
    """
    if link_origin not in ("direct", "derived"):
        raise BusinessTaxonomyError("link_origin must be 'direct' or 'derived'")
    _validate_link_kinds(taxonomy_type, target_type)
    org_id = _required(org_id, "org_id")
    project_id = _required(project_id, "project_id")
    taxonomy_id = _required(taxonomy_id, "taxonomy_id")
    target_id = _required(target_id, "target_id")
    relation_type = normalize_slug(relation_type, "relation_type")
    if relation_type not in ALLOWED_RELATION_TYPES:
        raise BusinessTaxonomyError(
            f"relation_type must be one of: {', '.join(sorted(ALLOWED_RELATION_TYPES))}"
        )
    _assert_project_org(conn, project_id=project_id, org_id=org_id)
    _assert_taxonomy_source(
        conn,
        org_id=org_id,
        taxonomy_type=taxonomy_type,
        taxonomy_id=taxonomy_id,
    )
    validate_target_reference(
        conn,
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        loaded_modules=loaded_modules,
    )
    link_id = _mint("blink")
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO app.mdm_business_links
                    (id, org_id, project_id, taxonomy_type, taxonomy_id,
                     target_type, target_id, relation_type, link_origin, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {", ".join(_LINK_COLUMNS)}
                """,
                (
                    link_id,
                    org_id,
                    project_id,
                    taxonomy_type,
                    taxonomy_id,
                    target_type,
                    target_id,
                    relation_type,
                    link_origin,
                    actor,
                ),
            )
            row = _dict_from_row(cur.fetchone(), _LINK_COLUMNS)
        _audit(
            conn,
            actor=actor,
            action=ACTION_BUSINESS_LINK_CREATED,
            org_id=org_id,
            resource_id=link_id,
            reason=reason,
            trace_id=trace_id,
            before=None,
            after=row,
        )
        return row
    except psycopg.errors.UniqueViolation as exc:
        raise DuplicateTaxonomySlugError("This governed business link already exists") from exc


def list_links(
    conn,
    *,
    org_id: str,
    project_id: str,
    taxonomy_id: str | None = None,
    include_retired: bool = False,
) -> list[dict[str, Any]]:
    """The links this Project holds. Withdrawn ones are excluded by default.

    ``include_retired`` is what keeps the promise the retirement makes: a list
    that claims to be complete never silently drops a row, so a caller that
    wants the withdrawn links gets them CARRYING their retirement rather than
    being told they never existed.
    """

    params: list[Any] = [org_id, project_id]
    taxonomy_clause = ""
    if taxonomy_id:
        taxonomy_clause = " AND taxonomy_id = %s"
        params.append(taxonomy_id)
    retired_clause = "" if include_retired else " AND retired_at IS NULL"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_LINK_COLUMNS)}
            FROM app.mdm_business_links
            WHERE org_id = %s AND project_id = %s{taxonomy_clause}{retired_clause}
            ORDER BY taxonomy_type, taxonomy_id, target_type, target_id, relation_type
            """,
            tuple(params),
        )
        links = [_dict_from_row(row, _LINK_COLUMNS) for row in cur.fetchall()]
    # THE WORD TRAVELS WITH THE LINK. Two console surfaces resolved it themselves
    # off whatever catalogue they had loaded and fell back to `bdom_<ULID>`; the
    # name is read here instead, in one statement per kind, and `None` says the
    # taxonomy node is no longer readable rather than naming it by its address.
    names = _taxonomy_names(conn, org_id=org_id, links=links)
    for link in links:
        link["taxonomy_name"] = names.get(str(link.get("taxonomy_id") or ""))
    return links


def retire_link(
    conn,
    *,
    org_id: str,
    project_id: str,
    link_id: str,
    actor: str,
    reason: str,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Withdraw a governed business link. The row stays; it stops being served.

    THIS USED TO BE A `DELETE`, and the rule it broke is already ratified one
    surface over: *"Retirement is a supersede, never a delete. An event that has
    been read is evidence"* (`context-hub.md`, amendment of 2026-08-17). A
    business link is read by more surfaces than an event is -- the Datastream
    workbench's business path, the Project's domain applicability, the Context
    Hub graph, and the used-by count of every Business Domain in the Governance
    collection -- so destroying the row loses the difference between "this link
    was never made" and "this link was made, and withdrawn". The second is the
    one an operator needs when a Datastream stops appearing under a domain.

    A retirement names its author and its reason. Migration 306 refuses the
    DELETE at the table, so removing it here repairs one caller and the guard
    repairs the class.

    Retiring an already-retired link is refused rather than silently repeated:
    the second retirement would overwrite the first author and reason, which is
    exactly the history this function exists to keep.
    """

    actor = _required(actor, "actor")
    reason = _required(reason, "reason")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_LINK_COLUMNS)}
            FROM app.mdm_business_links
            WHERE id = %s AND org_id = %s AND project_id = %s
            FOR UPDATE
            """,
            (link_id, org_id, project_id),
        )
        before = _dict_from_row(cur.fetchone(), _LINK_COLUMNS)
        if before["retired_at"] is not None:
            raise BusinessTaxonomyError("This governed business link is already withdrawn")
        cur.execute(
            f"""
            UPDATE app.mdm_business_links
               SET retired_at = NOW(), retired_by = %s, retired_reason = %s
             WHERE id = %s AND org_id = %s AND project_id = %s AND retired_at IS NULL
            RETURNING {", ".join(_LINK_COLUMNS)}
            """,
            (actor, reason, link_id, org_id, project_id),
        )
        after = _dict_from_row(cur.fetchone(), _LINK_COLUMNS)
    _audit(
        conn,
        actor=actor,
        action=ACTION_BUSINESS_LINK_RETIRED,
        org_id=org_id,
        resource_id=link_id,
        reason=reason,
        trace_id=trace_id,
        before=before,
        after=after,
    )
    return after



def taxonomy_hierarchy_edges(
    domains: list[dict[str, Any]],
    classifications: list[dict[str, Any]],
    *,
    project_id: str,
) -> list[dict[str, Any]]:
    """Build read-only graph edges from the authoritative taxonomy hierarchy."""
    domain_ids = {str(domain["id"]) for domain in domains}
    classification_by_id = {str(item["id"]): item for item in classifications}
    edges: list[dict[str, Any]] = []
    for item in sorted(classifications, key=lambda row: str(row["id"])):
        item_id = str(item["id"])
        domain_id = str(item["domain_id"])
        parent_id = str(item["parent_id"]) if item.get("parent_id") else None
        if parent_id:
            if parent_id not in classification_by_id:
                # Do not invent a direct domain relationship when a parent is
                # missing or archived; the child remains an honest orphan.
                continue
            from_id = parent_id
            from_type = "business_classification"
        elif domain_id in domain_ids:
            from_id = domain_id
            from_type = "business_domain"
        else:
            continue
        created_at = item.get("updated_at") or item.get("created_at") or ""
        if hasattr(created_at, "isoformat"):
            created_at = created_at.isoformat()
        edges.append(
            {
                "id": f"taxonomy_contains:{from_id}:{item_id}",
                "from_id": from_id,
                "from_type": from_type,
                "to_id": item_id,
                "to_type": "business_classification",
                "edge_type": "contains",
                "project_id": project_id,
                "created_by": item.get("created_by") or "auto",
                "created_at": str(created_at),
                "link_origin": "derived",
            }
        )
    return edges

#: Target types whose node the mindmap has to synthesize, and where to read its
#: real name from. `topic`, `procedure` and `schema_doc` are absent because the
#: graph bundle already composes those from their own stores; `target_field` is
#: absent because it is returned separately as `referenced_target_fields`.
#:
#: The fourth element is the SCOPE of the owner's table. Three of these are
#: project-owned; a canonical field exists at both scopes, and reading it with a
#: bare `project_id = %s` would silently drop the title of every platform field
#: -- the node would still appear, under its id, which is the failure mode this
#: module already refuses for an unreadable owner.
_PROJECT_SCOPE = "project"
_BOTH_SCOPES = "project_or_platform"
_SYNTHESIZED_TARGETS = {
    "datastream": ("app.datastreams", "name", "Governed Datastream", _PROJECT_SCOPE),
    "semantic_view": ("app.semantic_views", "name", "Published Semantic View", _PROJECT_SCOPE),
    "semantic_concept": ("app.semantic_concepts", "name", "Semantic Concept", _PROJECT_SCOPE),
    "canonical_field": (
        "app.mdm_canonical_fields",
        "canonical_name",
        "Canonical field",
        _BOTH_SCOPES,
    ),
}


def _owned_target_nodes(conn, links, *, project_id: str) -> list[dict[str, Any]]:
    """One node per linked Datastream / Semantic View / Concept / canonical field.

    Without this the mindmap silently loses the link. `graph_projection` emits an
    EDGE for every business link, and the graph bundle drops edges whose endpoint
    resolves to no node -- so a Datastream attached to a Business Domain would
    produce a link that exists in `mdm_business_links`, is returned by the API,
    and appears nowhere. That is the shape this repository calls a defect that
    looks like an empty state.

    The three types became linkable in the same wave as this function
    (`ee42bb8`); widening the target set without teaching the projection to
    represent them is what would have made the criterion look closed while the
    result was invisible.

    Titles are READ from each owner rather than derived from the id. The
    `report_view` block above title-cases its id because a report has no row to
    read; these have one, and showing `ds_01J...` in a mindmap is not a name.

    The SCOPE is read the same way, and for the same reason (AI-298). A canonical
    field exists at both scopes, so writing `project` on every node would tell a
    reader that a field the whole platform shares belongs to the project in front
    of them.
    """
    synthesized: list[dict[str, Any]] = []
    for target_type, (table, name_column, label, scope) in _SYNTHESIZED_TARGETS.items():
        ids = sorted({link["target_id"] for link in links if link["target_type"] == target_type})
        if not ids:
            continue
        scope_predicate = (
            "project_id = %s"
            if scope == _PROJECT_SCOPE
            else "(project_id IS NULL OR project_id = %s)"
        )
        titles: dict[str, str] = {}
        scopes: dict[str, str] = {}
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT id, {name_column}, project_id "  # noqa: S608 -- fixed table map
                    f"FROM {table} WHERE {scope_predicate} AND id = ANY(%s)",
                    (project_id, list(ids)),
                )
                for row in cur.fetchall():
                    titles[str(row[0])] = str(row[1])
                    scopes[str(row[0])] = "project" if row[2] else "platform"
        except Exception:  # noqa: BLE001 -- an unreadable owner must not hide the link
            titles = {}
            scopes = {}
        for target_id in ids:
            synthesized.append(
                {
                    "id": target_id,
                    "node_type": target_type,
                    # An owner that could not be read yields the id, never a
                    # guessed name: the node still appears, so the link stays
                    # visible, and it does not claim a title nobody supplied.
                    "title": titles.get(target_id, target_id),
                    "excerpt": f"{label} {target_id}",
                    "owner": None,
                    "version_number": 1,
                    # An unread owner falls back to `project`: the link was made
                    # from a project, so that is the one scope it is certain to
                    # be visible in. Guessing `platform` would widen it.
                    "scope": scopes.get(target_id, "project"),
                    "status": "active",
                }
            )
    return synthesized


def graph_projection(conn, *, org_id: str, project_id: str) -> dict[str, Any]:
    """Project the authoritative links into the existing Context Graph read model."""
    taxonomy = list_taxonomy(conn, org_id=org_id, status="active")
    links = list_links(conn, org_id=org_id, project_id=project_id)
    nodes: list[dict[str, Any]] = []
    for domain in taxonomy["domains"]:
        nodes.append(
            {
                "id": domain["id"],
                "node_type": "business_domain",
                "title": domain["name"],
                "excerpt": domain["description"][:280],
                "owner": domain["owner"] or domain["created_by"],
                "version_number": domain["version_number"],
                "scope": "organization",
                "status": domain["status"],
                "business_slug": domain["slug"],
            }
        )
    for item in taxonomy["classifications"]:
        nodes.append(
            {
                "id": item["id"],
                "node_type": "business_classification",
                "title": item["name"],
                "excerpt": item["description"][:280],
                "owner": item["owner"] or item["created_by"],
                "version_number": item["version_number"],
                "scope": "organization",
                "status": item["status"],
                "business_slug": item["slug"],
                "classification_type": item["classification_type"],
                "domain_id": item["domain_id"],
                "parent_id": item["parent_id"],
            }
        )
    report_ids = sorted(
        {link["target_id"] for link in links if link["target_type"] == "report_view"}
    )
    for report_id in report_ids:
        nodes.append(
            {
                "id": report_id,
                "node_type": "report_view",
                "title": report_id.split("/", 1)[1].replace("_", " ").replace("-", " ").title(),
                "excerpt": f"Governed report view {report_id}",
                "owner": None,
                "version_number": 1,
                "scope": "project",
                "status": "active",
                "view_kind": "report",
            }
        )
    nodes.extend(_owned_target_nodes(conn, links, project_id=project_id))
    edges = taxonomy_hierarchy_edges(
        taxonomy["domains"], taxonomy["classifications"], project_id=project_id
    ) + [
        {
            "id": link["id"],
            "from_id": link["taxonomy_id"],
            "from_type": link["taxonomy_type"],
            "to_id": link["target_id"],
            "to_type": link["target_type"],
            "edge_type": link["relation_type"],
            "project_id": project_id,
            "created_by": link["created_by"],
            "created_at": link["created_at"],
            "link_origin": "direct",
        }
        for link in links
    ]
    return {
        "nodes": nodes,
        "edges": edges,
        "referenced_target_fields": sorted(
            {link["target_id"] for link in links if link["target_type"] == "target_field"}
        ),
    }


def build_path_key(project_id: str, ordered_path: list[dict[str, Any]]) -> str:
    """Hash the exact ordered, versioned route the caller evaluated."""
    payload = {"project_id": _required(project_id, "project_id"), "path": ordered_path}
    digest = hashlib.sha256(serialize_snapshot(payload).encode("utf-8")).hexdigest()
    return f"ctxp_{digest[:32]}"


#: Node types that carry NO version of their own, each for a stated reason. A
#: `null` here is a fact about the object, not a version the snapshot failed to
#: read -- Story 45.1 M-1 already refused to coerce one into `1`.
#:
#: `report_view` is identified by its stable `<module>/<report_id>` key.
#: `datastream` has no version ledger for the object itself: its plan, mapping
#: and output are versioned, and none of them IS the Datastream.
#: `canonical_field` is a mutable-with-audit row of the MDM registry; the audit
#: trail says who changed it, and there is no version registry to read.
#:
#: THREE OF THESE WERE A LIVE DEFECT, found while adding the fourth (AI-298):
#: `datastream`, `semantic_view` and `semantic_concept` became linkable in the
#: 49.6 wave and were never added to the map below, so previewing the path of a
#: link to one of them raised `KeyError` -- a 500 on a governed read. Widening a
#: target set means walking every reader of it, and the test beside this now
#: drives all of them.
_UNVERSIONED_PATH_NODES = frozenset({"report_view", "datastream", "canonical_field"})


def _version_for_node(conn, *, node_type: str, node_id: str) -> int | None:
    """The version the path key is derived from, or None when there is none.

    Story 45.1 M-1: this returned a hard `1` for `report_view` and coerced a
    missing version row to `1` everywhere else, so "unversioned" and "at version
    one" produced the same `path_key` — and AC8's `path_version_drift` could not
    tell them apart. A report view is identified by the stable
    `<module>/<report_id>` key and carries no version of its own; the snapshot now
    says `null` instead of asserting a version it does not have.
    """
    if node_type in _UNVERSIONED_PATH_NODES:
        return None
    query_by_type = {
        # Story 49.2: the two business ledgers resolve through the catalogue, so
        # a path pinned on a NATIVELY created identity carries the version it
        # really has. Read from the superseded ledger alone it answered NULL --
        # which `_path_key` spells "unversioned", the same word a report view
        # gets -- and `path_version_drift` would then never fire for the objects
        # the authority governs.
        "business_domain": (
            "SELECT max(version_number) "
            f"FROM {catalogue.DOMAIN_VERSION_SOURCE} v WHERE v.domain_id = %s"
        ),
        "business_classification": (
            "SELECT max(version_number) "
            f"FROM {catalogue.CLASSIFICATION_VERSION_SOURCE} v "
            "WHERE v.classification_id = %s"
        ),
        "topic": (
            "SELECT max(version_number) "
            "FROM app.context_topics_versions WHERE topic_id = %s"
        ),
        "procedure": (
            "SELECT max(version_number) "
            "FROM app.procedures_versions WHERE procedure_id = %s"
        ),
        "schema_doc": (
            "SELECT max(version_number) "
            "FROM app.schema_context_versions WHERE schema_context_id = %s"
        ),
        "target_field": (
            "SELECT max(version_number) "
            "FROM app.target_fields_versions WHERE name = %s"
        ),
        # Read from the OBJECT's version ledger, keyed by the object id: a
        # business link pins the object, and the path key records which version
        # of it the caller evaluated.
        "semantic_view": (
            "SELECT max(version_number) "
            "FROM app.semantic_view_versions WHERE view_id = %s"
        ),
        "semantic_concept": (
            "SELECT max(version_number) "
            "FROM app.semantic_concept_versions WHERE concept_id = %s"
        ),
    }
    with conn.cursor() as cur:
        cur.execute(query_by_type[node_type], (node_id,))
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _path_node(
    conn,
    *,
    node_type: str,
    node_id: str,
    slug: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    node = {
        "kind": "node",
        "node_type": node_type,
        "id": node_id,
        "version_number": _version_for_node(conn, node_type=node_type, node_id=node_id),
    }
    if slug:
        node["slug"] = slug
    if title:
        node["title"] = title
    return node


def _load_direct_path(
    conn,
    *,
    org_id: str,
    project_id: str,
    target_type: str,
    target_id: str,
    taxonomy_id: str | None = None,
) -> list[dict[str, Any]] | None:
    params: list[Any] = [org_id, project_id, target_type, target_id]
    taxonomy_clause = ""
    if taxonomy_id:
        taxonomy_clause = " AND taxonomy_id = %s"
        params.append(taxonomy_id)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_LINK_COLUMNS)}
            FROM app.mdm_business_links
            WHERE org_id = %s AND project_id = %s
              AND target_type = %s AND target_id = %s{taxonomy_clause}
              AND retired_at IS NULL
            ORDER BY taxonomy_type, taxonomy_id, relation_type, id
            LIMIT 1
            """,
            tuple(params),
        )
        raw = cur.fetchone()
    if raw is None:
        return None
    link = _dict_from_row(raw, _LINK_COLUMNS)

    classification_nodes: list[dict[str, Any]] = []
    if link["taxonomy_type"] == "business_domain":
        domain = get_domain(conn, org_id=org_id, domain_id=link["taxonomy_id"])
    else:
        current = get_classification(conn, org_id=org_id, classification_id=link["taxonomy_id"])
        while True:
            classification_nodes.append(
                _path_node(
                    conn,
                    node_type="business_classification",
                    node_id=current["id"],
                    slug=current["slug"],
                    title=current["name"],
                )
            )
            if current["parent_id"] is None:
                break
            current = get_classification(
                conn, org_id=org_id, classification_id=current["parent_id"]
            )
        classification_nodes.reverse()
        domain = get_domain(conn, org_id=org_id, domain_id=current["domain_id"])

    ordered = [
        _path_node(
            conn,
            node_type="business_domain",
            node_id=domain["id"],
            slug=domain["slug"],
            title=domain["name"],
        ),
        *classification_nodes,
        {
            "kind": "edge",
            "id": link["id"],
            "edge_type": link["relation_type"],
            "link_origin": "direct",
        },
        _path_node(conn, node_type=target_type, node_id=target_id),
    ]
    return ordered


def preview_business_path(
    conn,
    *,
    org_id: str,
    project_id: str,
    target_type: str,
    target_id: str,
    taxonomy_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve a route for the admin drawer without recording downstream use."""
    if target_type not in BUSINESS_TARGET_TYPES:
        raise BusinessTaxonomyError("Unknown governed target type")
    ordered_path = _load_direct_path(
        conn,
        org_id=org_id,
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        taxonomy_id=taxonomy_id,
    )
    if not ordered_path:
        return None
    versions = [
        {
            "node_type": segment["node_type"],
            "id": segment["id"],
            "version_number": segment["version_number"],
        }
        for segment in ordered_path
        if segment.get("kind") == "node"
    ]
    return {
        "path_key": build_path_key(project_id, ordered_path),
        "target": {"type": target_type, "id": target_id},
        "link_origin": "direct",
        "ordered_path": ordered_path,
        "source_versions": versions,
    }


def _persist_resolved_path(
    conn,
    *,
    org_id: str,
    project_id: str,
    ordered_path: list[dict[str, Any]],
    purpose: str,
    target_type: str,
    target_id: str,
    link_origin: str,
    actor: str,
    trace_id: str | None,
) -> dict[str, Any]:
    if purpose not in {"llm", "evaluation", "report"}:
        raise BusinessTaxonomyError("purpose must be llm, evaluation or report")
    if link_origin not in {"direct", "derived"}:
        raise BusinessTaxonomyError("link_origin must be direct or derived")
    path_key = build_path_key(project_id, ordered_path)
    path_id = _mint("path")
    versions = [
        {
            "node_type": segment["node_type"],
            "id": segment["id"],
            "version_number": segment["version_number"],
        }
        for segment in ordered_path
        if segment.get("kind") == "node"
    ]
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.context_path_resolutions
                (id, org_id, project_id, path_key, purpose, trace_id,
                 target_type, target_id, link_origin, ordered_path,
                 source_versions, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s)
            """,
            (
                path_id,
                org_id,
                project_id,
                path_key,
                purpose,
                trace_id,
                target_type,
                target_id,
                link_origin,
                serialize_snapshot(ordered_path),
                serialize_snapshot(versions),
                _required(actor, "actor"),
            ),
        )
    return {
        "id": path_id,
        "path_key": path_key,
        "purpose": purpose,
        "trace_id": trace_id,
        "target": {"type": target_type, "id": target_id},
        "link_origin": link_origin,
        "ordered_path": ordered_path,
        "source_versions": versions,
    }


def resolve_business_path(
    conn,
    *,
    org_id: str,
    project_id: str,
    target_type: str,
    target_id: str,
    purpose: str,
    actor: str,
    trace_id: str | None,
    taxonomy_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve and persist the exact direct business route used by a caller."""
    if target_type not in BUSINESS_TARGET_TYPES:
        raise BusinessTaxonomyError("Unknown governed target type")
    ordered_path = _load_direct_path(
        conn,
        org_id=org_id,
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        taxonomy_id=taxonomy_id,
    )
    if not ordered_path:
        return None
    return _persist_resolved_path(
        conn,
        org_id=org_id,
        project_id=project_id,
        ordered_path=ordered_path,
        purpose=purpose,
        target_type=target_type,
        target_id=target_id,
        link_origin="direct",
        actor=actor,
        trace_id=trace_id,
    )


def resolve_report_paths(
    conn,
    *,
    org_id: str,
    project_id: str,
    report_id: str,
    actor: str,
    trace_id: str | None,
    loaded_modules=None,
) -> list[dict[str, Any]]:
    """Persist direct and target-field-derived business routes for a report."""
    if report_id.count("/") != 1:
        raise BusinessTaxonomyError("report_id must be <module>/<report_id>")

    results: list[dict[str, Any]] = []
    direct_path = _load_direct_path(
        conn,
        org_id=org_id,
        project_id=project_id,
        target_type="report_view",
        target_id=report_id,
    )
    if direct_path:
        results.append(
            _persist_resolved_path(
                conn,
                org_id=org_id,
                project_id=project_id,
                ordered_path=direct_path,
                purpose="report",
                target_type="report_view",
                target_id=report_id,
                link_origin="direct",
                actor=actor,
                trace_id=trace_id,
            )
        )

    from core.report_chain import get_report_chain  # noqa: PLC0415

    module_name, local_report_id = report_id.split("/", 1)
    chain = get_report_chain(
        project_id,
        module_name,
        local_report_id,
        conn,
        loaded_modules=loaded_modules,
    )
    if not chain:
        return results

    seen_keys = {item["path_key"] for item in results}
    seen_fields: set[str] = set()
    for metric in chain.get("metrics") or []:
        target_field = metric.get("target_field") or {}
        field_name = target_field.get("name")
        if not field_name or field_name in seen_fields:
            continue
        seen_fields.add(field_name)
        field_path = _load_direct_path(
            conn,
            org_id=org_id,
            project_id=project_id,
            target_type="target_field",
            target_id=field_name,
        )
        if not field_path:
            continue
        ordered_path = [
            *field_path,
            {
                "kind": "edge",
                "id": f"derived:{report_id}:{field_name}",
                "edge_type": "feeds_report",
                "link_origin": "derived",
            },
            _path_node(conn, node_type="report_view", node_id=report_id),
        ]
        resolved = _persist_resolved_path(
            conn,
            org_id=org_id,
            project_id=project_id,
            ordered_path=ordered_path,
            purpose="report",
            target_type="report_view",
            target_id=report_id,
            link_origin="derived",
            actor=actor,
            trace_id=trace_id,
        )
        if resolved["path_key"] not in seen_keys:
            results.append(resolved)
            seen_keys.add(resolved["path_key"])
    return results
