"""The fourteen readers answer from the AUTHORITY first, on a real database (49.2).

WHY THIS FILE IS pg-GATED AND NOT MOCKED. Everything the catalogue does is SQL:
a `UNION ALL` whose halves must not double-count a converged identity, a
`NOT EXISTS` that decides which store answers, a `LEFT JOIN` onto the current
node version, and a `published_at IS NOT NULL` that decides what counts as
history. A `MagicMock` cursor accepts any string and returns whatever the test
already believed, so a mocked version of this file would assert that the code
is the code. The order is proved by MUTATING the authority and watching the
reader follow.

THE TWO ORGANIZATIONS, because AC5 asks for both. One is CONVERGED and then
mints a Business Domain natively -- the identity that made
`governance.md` § Decision 2 record its own open debt: *"a natively created
identity reads `None` there [the Versions facet] until the lens is re-pointed"*.
The other has NOT converged, and is the reason the fallback exists at all: its
six seeded domains must keep rendering exactly as they did, or "nothing changes
for a person" is false.

WHAT EACH TEST WOULD CATCH. Every one of them fails on a specific, nameable
reversal:

  * `test_a_converged_identity_is_answered_once` fails if the `NOT EXISTS`
    guard is dropped -- every converged Business Domain would appear TWICE in
    both Governance lenses and in every picker;
  * `test_the_authority_answers_before_the_projection` fails if the two halves
    are unioned in the other order, or if the guard is put on the authority half
    instead -- a renamed identity would keep serving the name its projection was
    born with;
  * `test_a_not_yet_converged_domain_still_resolves` fails if the fallback is
    dropped, which is the whole risk of this change: it would RETIRE identities
    rather than add them;
  * `test_the_versions_facet_of_a_native_identity_is_not_none` is AC3 itself;
  * `test_a_draft_revision_is_not_history` fails if `published_at IS NOT NULL`
    becomes `TRUE`, which would show an unapproved revision as an approval;
  * `test_the_pin_validators_accept_a_native_identity` fails if any of the three
    pin guards is left on the legacy version ledger -- each of them refuses a pin
    on an identity the product itself just minted;
  * `test_an_unknown_id_is_answered_by_nobody` fails if the resolver ever
    invents a label, a parent or a lifecycle.

Rolled back by the `live_postgres` fixture: nothing this file writes survives it.
"""

from __future__ import annotations

import uuid

import pytest

from tests.integration.taxonomy_fixtures import insert_domain_fixture

pytestmark = pytest.mark.pg_owner

ACTOR = "owner@example.com"


# ---------------------------------------------------------------------------
# Fixtures. An organization arrives with six seeded Business Domains
# (`trg_organizations_seed_business_domains`, migration 130), so the
# not-yet-converged half of every assertion is the product's own rows.
# ---------------------------------------------------------------------------


def _new_org(conn) -> dict[str, str]:
    suffix = uuid.uuid4().hex[:10]
    org_id = f"org_{suffix}"
    project_id = f"proj_{suffix}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (org_id, f"Org {suffix}", f"org-{suffix}", ACTOR),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (project_id, org_id, f"Project {suffix}", f"project-{suffix}", ACTOR),
        )
    return {"org_id": org_id, "project_id": project_id, "suffix": suffix}


@pytest.fixture()
def unconverged(live_postgres) -> dict[str, str]:
    """An organization whose taxonomy is still held by the superseded store."""
    return _new_org(live_postgres)


@pytest.fixture()
def converged(live_postgres) -> dict[str, str]:
    """An organization that converged, so it can mint identities natively."""
    from core.master_data_convergence import converge_org_taxonomy

    scope = _new_org(live_postgres)
    converge_org_taxonomy(
        live_postgres,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        actor=ACTOR,
        idempotency_key=f"conv_{uuid.uuid4().hex}",
        reason="AC1: one authority",
    )
    return scope


def _append_legacy_revisions(conn, *, domain_id: str, upto: int) -> None:
    """Give a domain the version HISTORY the seeded fixture does not have.

    Migration 130 seeds one `seeded` version per starter domain, which is why
    every earlier assertion in this file was blind to the defect review round 1
    found: with a single revision, losing the ledger and keeping the authority's
    own version 1 produce the same count. A row here is what a rename through the
    superseded writers used to leave behind -- an INSERT into an append-only
    ledger, which is the only write that table still accepts.
    """
    with conn.cursor() as cur:
        for number in range(2, upto + 1):
            cur.execute(
                """
                INSERT INTO app.mdm_business_domain_versions
                    (domain_id, version_number, org_id, slug, name, description,
                     owner, status, created_by, created_at, updated_at,
                     archived_at, change_kind, changed_by)
                SELECT id, %s, org_id, slug, name, description, owner, status,
                       created_by, created_at, updated_at, archived_at,
                       'updated', %s
                  FROM app.mdm_business_domains
                 WHERE id = %s
                """,
                (number, ACTOR, domain_id),
            )


def _create_native_domain(conn, scope, *, name: str = "Retail Media") -> str:
    """Mint a Business Domain the way the product mints one, and return its id."""
    from core.master_data_commands import create_business_identity

    result = create_business_identity(
        conn,
        project_id=scope["project_id"],
        org_id=scope["org_id"],
        kind="business_domain",
        name=name,
        actor=ACTOR,
        idempotency_key=f"create_{uuid.uuid4().hex}",
        reason="a Business Domain minted in the authority",
    )
    return str(result["result"]["node_id"])


# ---------------------------------------------------------------------------
# AC1 -- one resolver, authority first, abstention stated in the payload.
# ---------------------------------------------------------------------------


def test_a_native_identity_is_answered_by_the_authority(live_postgres, converged):
    from core import business_identity_catalogue as catalogue

    node_id = _create_native_domain(live_postgres, converged)
    identity = catalogue.resolve_domain(
        live_postgres, org_id=converged["org_id"], domain_id=node_id
    )

    assert identity is not None
    assert identity["source"] == catalogue.AUTHORITY
    assert identity["name"] == "Retail Media"
    assert identity["slug"] == "retail-media"
    assert identity["status"] == "active"


def test_a_not_yet_converged_domain_still_resolves(live_postgres, unconverged):
    """The fallback. Dropping it would RETIRE identities rather than add them."""
    from core import business_identity_catalogue as catalogue

    domains = catalogue.resolve_domains(live_postgres, org_id=unconverged["org_id"])

    assert len(domains) == 6, "migration 130 seeds six starter domains"
    assert {row["source"] for row in domains.values()} == {catalogue.SUPERSEDED}
    assert {row["slug"] for row in domains.values()} == {
        "sales",
        "marketing",
        "product",
        "engineering",
        "finance",
        "legal",
    }


def test_a_converged_identity_is_answered_once(live_postgres, converged):
    """One business object, one row -- whichever store still holds a copy of it.

    Every converged domain has a node AND a stamped legacy row. Without the
    `NOT EXISTS` guard the union answers twice, and both Governance lenses, the
    picker and the Context Hub graph would show every identity in duplicate.
    """
    from core import business_identity_catalogue as catalogue

    domains = catalogue.resolve_domains(live_postgres, org_id=converged["org_id"])

    assert len(domains) == 6
    assert {row["source"] for row in domains.values()} == {catalogue.AUTHORITY}


def test_the_authority_answers_before_the_projection(live_postgres, converged):
    """MUTATE the projection, and the reader must not follow it.

    The born-superseded row is written by the same command that writes the node,
    so the two agree in production. Making them DISAGREE here is the only way to
    prove which one is read: a reader that answers `Renamed In The Old Store`
    has the union the wrong way round.
    """
    from core import business_identity_catalogue as catalogue

    node_id = _create_native_domain(live_postgres, converged)
    with live_postgres.cursor() as cur:
        cur.execute(
            "UPDATE app.mdm_business_domains SET name = %s, status = 'archived' "
            "WHERE id = %s AND org_id = %s",
            ("Renamed In The Old Store", node_id, converged["org_id"]),
        )

    identity = catalogue.resolve_domain(
        live_postgres, org_id=converged["org_id"], domain_id=node_id
    )
    assert identity["name"] == "Retail Media"
    assert identity["status"] == "active"
    assert identity["source"] == catalogue.AUTHORITY


def test_an_unknown_id_is_answered_by_nobody(live_postgres, unconverged):
    """No label, no parent, no lifecycle. The caller keeps its own empty state."""
    from core import business_identity_catalogue as catalogue

    assert (
        catalogue.resolve_domain(
            live_postgres, org_id=unconverged["org_id"], domain_id="bd_nothing_holds_this"
        )
        is None
    )
    assert (
        catalogue.resolve_classification(
            live_postgres,
            org_id=unconverged["org_id"],
            classification_id="bcl_nothing_holds_this",
        )
        is None
    )


def test_an_identity_is_never_borrowed_from_another_organization(
    live_postgres, converged, unconverged
):
    from core import business_identity_catalogue as catalogue

    node_id = _create_native_domain(live_postgres, converged)

    assert (
        catalogue.resolve_domain(
            live_postgres, org_id=unconverged["org_id"], domain_id=node_id
        )
        is None
    )


# ---------------------------------------------------------------------------
# AC3 -- the Versions facet reads `app.master_data_object_versions`.
# ---------------------------------------------------------------------------


def test_the_versions_facet_of_a_native_identity_is_not_none(live_postgres, converged):
    """The debt `governance.md` § Decision 2 recorded against itself, closed."""
    from core.governance_read_model import _business_domains_lens

    node_id = _create_native_domain(live_postgres, converged)
    lens = _business_domains_lens(
        live_postgres, converged["project_id"], converged["org_id"], None
    )

    item = next(
        entry for entry in lens.items if entry["object_ref"]["id"] == node_id
    )
    assert item["versions"]["state"] == "available"
    assert item["versions"]["count"] == 1
    assert item["active_version_ref"]["version"] == 1
    assert [ref["version"] for ref in item["versions"]["refs"]] == [1]


def test_a_draft_revision_is_not_history(live_postgres, converged):
    """Only a PUBLISHED revision counts. A draft is not an approval.

    `published_at IS NOT NULL` is the whole guard; relaxing it would put an
    unapproved revision into the one facet a person reads approval from.
    """
    from core import business_identity_catalogue as catalogue
    from core.master_data import create_node_version, fetch_org_node, list_node_versions

    node_id = _create_native_domain(live_postgres, converged)
    node = fetch_org_node(live_postgres, org_id=converged["org_id"], node_id=node_id)
    published = list_node_versions(
        live_postgres, org_id=converged["org_id"], node_id=node_id
    )
    create_node_version(
        live_postgres,
        org_id=converged["org_id"],
        registry_id=str(node["registry_id"]),
        node_id=node_id,
        payload={
            "slug": "retail-media",
            "name": "Retail Media",
            "description": "a revision nobody approved",
            "owner": None,
            "lifecycle_state": "active",
        },
        type_version_id=str(published[0]["type_version_id"]),
        actor=ACTOR,
    )

    versions = catalogue.domain_versions(
        live_postgres, org_id=converged["org_id"], domain_id=node_id
    )
    assert [row["version_number"] for row in versions] == [1]
    assert {row["source"] for row in versions} == {catalogue.AUTHORITY}


def test_the_legacy_ledger_still_answers_for_an_unconverged_row(
    live_postgres, unconverged
):
    from core import business_identity_catalogue as catalogue
    from core.business_taxonomy import list_taxonomy

    domain_id = str(list_taxonomy(live_postgres, org_id=unconverged["org_id"])["domains"][0]["id"])
    versions = catalogue.domain_versions(
        live_postgres, org_id=unconverged["org_id"], domain_id=domain_id
    )

    assert [row["version_number"] for row in versions] == [1]
    assert versions[0]["source"] == catalogue.SUPERSEDED


# ---------------------------------------------------------------------------
# AC5 -- nothing changes for a person, and the pins stop refusing.
# ---------------------------------------------------------------------------


def test_the_pin_validators_accept_a_native_identity(live_postgres, converged):
    """Three guards, one fact. Each of them refused what the product just minted."""
    from core.feedback_review import _resolve_business_domain
    from core.golden_questions import _check_business_domain_pin
    from core.semantic_model import _validate_business_domain_refs

    node_id = _create_native_domain(live_postgres, converged)

    refusals: list = []
    pinned = _check_business_domain_pin(
        live_postgres,
        org_id=converged["org_id"],
        payload={
            "business_domain_id": node_id,
            "business_domain_version_number": 1,
        },
        refusals=refusals,
    )
    assert refusals == []
    assert pinned[0] == node_id and pinned[1] == 1

    assert _resolve_business_domain(
        live_postgres, org_id=converged["org_id"], domain_id=node_id, version_number=1
    ) == (node_id, 1)

    assert (
        _validate_business_domain_refs(
            live_postgres, converged["project_id"], [node_id], path="business_domain_refs"
        )
        == []
    )


def test_an_unknown_domain_is_still_refused_by_the_reference_guard(
    live_postgres, converged
):
    """The other direction. A resolver that resolved everything would guard nothing."""
    from core.semantic_model import _validate_business_domain_refs

    refusals = _validate_business_domain_refs(
        live_postgres,
        converged["project_id"],
        ["bd_no_such_identity"],
        path="business_domain_refs",
    )
    assert [refusal.code for refusal in refusals] == ["unknown_business_domain"]


def test_the_two_lenses_render_the_same_identities_before_and_after_convergence(
    live_postgres, unconverged
):
    """AC5, measured on ONE organization crossing the line.

    The Business Domain lens is read, the organization converges, and it is read
    again. Same ids, same labels, same lifecycle -- AND SAME HISTORY, which is
    the half review round 1 caught this test not measuring. On a fixture where
    every domain carries exactly one revision, a version count that collapsed to
    the authority's own version 1 was indistinguishable from one that survived;
    the domain below carries THREE, so the collapse has somewhere to show.

    RED BEFORE THE FIX: keyed on node existence, the version fallback dropped the
    whole legacy ledger the moment a node appeared, and this read
    `versions.count == 1` and `active_version_ref.version == 1` after the
    convergence against 3 before it.

    AMENDED 2026-08-31 (AI-324). "Same history" is the criterion, and it means
    NOTHING IS LOST -- it never meant that the convergence is invisible. Since
    the authority mints above the legacy maximum, converging this domain adds its
    own snapshot as revision 4: the three legacy revisions are all still there,
    still at 1, 2 and 3, and the identity now has four. What would still be red is
    the collapse this test was written for -- a count that drops to 1, or an
    active version that goes backwards.
    """
    from core import business_identity_catalogue as catalogue
    from core.business_taxonomy import _version_for_node, list_taxonomy
    from core.governance_read_model import _business_domains_lens
    from core.master_data_convergence import converge_org_taxonomy

    with_history = str(
        list_taxonomy(live_postgres, org_id=unconverged["org_id"])["domains"][0]["id"]
    )
    _append_legacy_revisions(live_postgres, domain_id=with_history, upto=3)

    def _seen() -> list[tuple[str, str, str, int, int]]:
        lens = _business_domains_lens(
            live_postgres, unconverged["project_id"], unconverged["org_id"], None
        )
        return sorted(
            (
                str(item["object_ref"]["id"]),
                str(item["object_ref"]["label"]),
                str(item["lifecycle_status"]),
                int(item["versions"]["count"]),
                int(item["active_version_ref"]["version"]),
            )
            for item in lens.items
        )

    def _taxonomy_version() -> int:
        return next(
            int(row["version_number"])
            for row in list_taxonomy(live_postgres, org_id=unconverged["org_id"])["domains"]
            if str(row["id"]) == with_history
        )

    before = _seen()
    assert len(before) == 6
    assert [row[3:] for row in before if row[0] == with_history] == [(3, 3)], before
    assert _taxonomy_version() == 3

    converge_org_taxonomy(
        live_postgres,
        org_id=unconverged["org_id"],
        project_id=unconverged["project_id"],
        actor=ACTOR,
        idempotency_key=f"conv_{uuid.uuid4().hex}",
        reason="AC5: the same identities, before and after",
    )

    after = _seen()
    assert [row[:3] for row in after] == [row[:3] for row in before], (
        "same ids, same labels, same lifecycle -- that half is unconditional"
    )
    # Every domain gains the convergence's own snapshot, and loses nothing: the
    # five with one legacy revision go to 2, the one with three goes to 4.
    assert [row[3:] for row in after if row[0] == with_history] == [(4, 4)], after
    assert {row[3:] for row in after if row[0] != with_history} == {(2, 2)}, after

    # And every reader of the same fact agrees, because they read one resolver.
    assert [
        row["version_number"]
        for row in catalogue.domain_versions(
            live_postgres, org_id=unconverged["org_id"], domain_id=with_history
        )
    ] == [4, 3, 2, 1]
    assert catalogue.domain_version_exists(
        live_postgres,
        org_id=unconverged["org_id"],
        domain_id=with_history,
        version_number=3,
    ), "the legacy revisions a pin may name are all still readable"
    assert _taxonomy_version() == 4
    assert (
        _version_for_node(
            live_postgres, node_type="business_domain", node_id=with_history
        )
        == 4
    )


def test_a_pin_at_the_real_version_survives_the_convergence(live_postgres, unconverged):
    """The user-visible half of the same defect (review round 1, finding 2).

    A Golden Question pins `(business_domain_id, version_number)`. Converging the
    organization used to make `_check_business_domain_pin` refuse version 3 --
    `unknown_business_domain_version`, about a version the organization plainly
    holds and had already accepted -- while `_version_for_node` answered 1 for the
    same object, so every AI path pinned at the real version read as drifted.

    RED BEFORE THE FIX: the second `_pin(3)` came back
    `['unknown_business_domain_version']`, and the options list offered
    `latest_version_number = 1`.
    """
    from core.business_taxonomy import list_taxonomy
    from core.golden_questions import _check_business_domain_pin, golden_question_options
    from core.master_data_convergence import converge_org_taxonomy

    domain_id = str(
        list_taxonomy(live_postgres, org_id=unconverged["org_id"])["domains"][0]["id"]
    )
    _append_legacy_revisions(live_postgres, domain_id=domain_id, upto=3)

    def _pin(number: int) -> list[str]:
        refusals: list = []
        _check_business_domain_pin(
            live_postgres,
            org_id=unconverged["org_id"],
            payload={
                "business_domain_id": domain_id,
                "business_domain_version_number": number,
            },
            refusals=refusals,
        )
        return [refusal.code for refusal in refusals]

    def _offered() -> int:
        rows = golden_question_options(
            live_postgres,
            org_id=unconverged["org_id"],
            project_id=unconverged["project_id"],
        )["business_domains"]
        return next(row["latest_version_number"] for row in rows if row["id"] == domain_id)

    assert _pin(3) == []
    assert _offered() == 3

    converge_org_taxonomy(
        live_postgres,
        org_id=unconverged["org_id"],
        project_id=unconverged["project_id"],
        actor=ACTOR,
        idempotency_key=f"conv_{uuid.uuid4().hex}",
        reason="the pin must survive",
    )

    assert _pin(3) == [], "a convergence must not invalidate a pin it did not change"
    # AI-324: the convergence mints ABOVE the legacy maximum, so the identity now
    # has a fourth revision -- the authority's snapshot -- and that is what the
    # options list offers as its latest. The pin at 3 keeps resolving, which is
    # the property this test exists for: what was accepted stays accepted.
    assert _offered() == 4
    assert _pin(4) == []
    # A revision NEITHER store holds is still refused: the fallback widens the set
    # that resolves, it never makes the guard answer yes to anything.
    assert _pin(9) == ["unknown_business_domain_version"]


def test_an_archived_node_reads_archived_through_every_reader(live_postgres, converged):
    """One lifecycle, derived from `archived_at` -- never a second stored word."""
    from core import business_identity_catalogue as catalogue
    from core.master_data import archive_org_node

    node_id = _create_native_domain(live_postgres, converged)
    archive_org_node(live_postgres, org_id=converged["org_id"], node_id=node_id)

    identity = catalogue.resolve_domain(
        live_postgres, org_id=converged["org_id"], domain_id=node_id
    )
    assert identity["status"] == "archived"
    assert identity["archived_at"] is not None
    assert node_id not in catalogue.resolve_domains(
        live_postgres, org_id=converged["org_id"], status="active"
    )


def test_a_domain_is_findable_by_slug_the_day_it_is_minted(live_postgres, converged):
    """What `context_seed` needs: no legacy row is required for a link to exist."""
    from core import business_identity_catalogue as catalogue

    node_id = _create_native_domain(live_postgres, converged, name="Retail Media")
    found = catalogue.domain_by_slug(
        live_postgres, org_id=converged["org_id"], slug="retail-media"
    )

    assert found is not None
    assert found["id"] == node_id
    assert found["source"] == catalogue.AUTHORITY


def test_a_superseded_row_answers_only_where_no_node_holds_the_id(
    live_postgres, unconverged
):
    """A fixture row this store still holds alone keeps every column it had."""
    from core import business_identity_catalogue as catalogue

    row = insert_domain_fixture(
        live_postgres,
        org_id=unconverged["org_id"],
        slug="probe-domain",
        name="Probe Domain",
        actor=ACTOR,
    )
    identity = catalogue.resolve_domain(
        live_postgres, org_id=unconverged["org_id"], domain_id=str(row["id"])
    )

    assert identity["source"] == catalogue.SUPERSEDED
    assert identity["name"] == "Probe Domain"
    assert identity["slug"] == "probe-domain"
    assert identity["created_by"] == ACTOR


# ---------------------------------------------------------------------------
# AI-324 -- a version number designates ONE content, ever.
#
# `governance.md`, amendment of 2026-08-31. Until it landed, the authority's
# counter restarted at 1 for a converged identity while the resolver unioned the
# two ledgers per `(id, version_number)`, so one number could name two contents
# and the union silently kept one of them. These four tests hold the repair from
# both ends: the writer mints above the union, and the resolver SHOUTS if it ever
# has a choice to make again.
# ---------------------------------------------------------------------------


def _force_colliding_authority_revision(conn, *, org_id: str, node_id: str, number: int) -> str:
    """Write an authority revision at a number the legacy ledger already spent.

    THE WRITER CAN NO LONGER PRODUCE THIS, which is the point: the only way to
    prove the resolver still reports the state is to reach around the writer and
    put the row there by hand. It copies the identity's current revision, so the
    row is well-formed in every respect except the one under test.
    """
    from core.master_data import _mint

    version_id = _mint("mdver")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.master_data_object_versions
                (id, org_id, project_id, registry_id, node_id, version_number,
                 status, vocabulary_version_id, type_version_id, payload,
                 content_hash, published_at, published_by, created_by)
            SELECT %s, org_id, project_id, registry_id, node_id, %s,
                   'superseded', vocabulary_version_id, type_version_id, payload,
                   content_hash, NOW(), %s, created_by
              FROM app.master_data_object_versions
             WHERE org_id = %s AND node_id = %s AND status = 'current'
            """,
            (version_id, number, ACTOR, org_id, node_id),
        )
        assert cur.rowcount == 1, "the identity must have a current revision to copy"
    return version_id


def test_the_authority_mints_above_the_legacy_maximum(live_postgres, unconverged):
    """Legacy v1..v3, then the convergence mints v4 -- never a second v1.

    RED BEFORE AI-324: `create_node_version` read its own ledger alone, so this
    minted version 1 for content that legacy version 1 does not hold, and the two
    were indistinguishable to every reader of a `(id, version_number)` pair.
    """
    from core import business_identity_catalogue as catalogue
    from core.business_taxonomy import list_taxonomy
    from core.master_data import list_node_versions
    from core.master_data_convergence import converge_org_taxonomy

    with_history = str(
        list_taxonomy(live_postgres, org_id=unconverged["org_id"])["domains"][0]["id"]
    )
    _append_legacy_revisions(live_postgres, domain_id=with_history, upto=3)

    converge_org_taxonomy(
        live_postgres,
        org_id=unconverged["org_id"],
        project_id=unconverged["project_id"],
        actor=ACTOR,
        idempotency_key=f"conv_{uuid.uuid4().hex}",
        reason="AI-324: the authority numbers from the legacy maximum",
    )

    minted = list_node_versions(
        live_postgres, org_id=unconverged["org_id"], node_id=with_history
    )
    assert [row["version_number"] for row in minted] == [4]

    history = catalogue.domain_versions(
        live_postgres, org_id=unconverged["org_id"], domain_id=with_history
    )
    assert [row["version_number"] for row in history] == [4, 3, 2, 1]
    assert [row["source"] for row in history] == [
        catalogue.AUTHORITY,
        catalogue.SUPERSEDED,
        catalogue.SUPERSEDED,
        catalogue.SUPERSEDED,
    ], "the convergence adds a revision to the history; it replaces none of it"


def test_a_revision_after_the_convergence_keeps_counting_up(live_postgres, unconverged):
    """v4 at the convergence, v5 at the next rename. The counter never goes back."""
    from core import business_identity_catalogue as catalogue
    from core.business_taxonomy import list_taxonomy
    from core.master_data import current_node_version_id
    from core.master_data_commands import run_node_command
    from core.master_data_convergence import converge_org_taxonomy

    with_history = str(
        list_taxonomy(live_postgres, org_id=unconverged["org_id"])["domains"][0]["id"]
    )
    _append_legacy_revisions(live_postgres, domain_id=with_history, upto=3)
    converge_org_taxonomy(
        live_postgres,
        org_id=unconverged["org_id"],
        project_id=unconverged["project_id"],
        actor=ACTOR,
        idempotency_key=f"conv_{uuid.uuid4().hex}",
        reason="AI-324",
    )
    live_postgres.commit()

    run_node_command(
        live_postgres,
        project_id=unconverged["project_id"],
        org_id=unconverged["org_id"],
        node_id=with_history,
        action="rename",
        actor=ACTOR,
        idempotency_key=f"mdn_{uuid.uuid4().hex}",
        label="Sales And Revenue",
        expected_version=current_node_version_id(
            live_postgres, org_id=unconverged["org_id"], node_id=with_history
        ),
    )

    assert [
        row["version_number"]
        for row in catalogue.domain_versions(
            live_postgres, org_id=unconverged["org_id"], domain_id=with_history
        )
    ] == [5, 4, 3, 2, 1]


def test_a_number_that_designates_two_contents_is_shouted_about(
    live_postgres, unconverged, caplog
):
    """The authority still answers -- and the operator is told a choice was made.

    This is the branch `governance.md` used to record as *"the authority wins a
    collision"*, silently. It cannot be produced by the product any more, so the
    row is planted by hand; what is measured is that the door does not swallow it.
    """
    import logging

    from core import business_identity_catalogue as catalogue
    from core.business_taxonomy import list_taxonomy
    from core.master_data_convergence import converge_org_taxonomy

    with_history = str(
        list_taxonomy(live_postgres, org_id=unconverged["org_id"])["domains"][0]["id"]
    )
    _append_legacy_revisions(live_postgres, domain_id=with_history, upto=3)
    converge_org_taxonomy(
        live_postgres,
        org_id=unconverged["org_id"],
        project_id=unconverged["project_id"],
        actor=ACTOR,
        idempotency_key=f"conv_{uuid.uuid4().hex}",
        reason="AI-324",
    )
    _force_colliding_authority_revision(
        live_postgres, org_id=unconverged["org_id"], node_id=with_history, number=2
    )

    with caplog.at_level(logging.ERROR, logger="core.business_identity_catalogue"):
        history = catalogue.domain_versions(
            live_postgres, org_id=unconverged["org_id"], domain_id=with_history
        )

    shouted = [
        record
        for record in caplog.records
        if catalogue.VERSION_NUMBER_COLLISION_CODE in record.getMessage()
    ]
    assert len(shouted) == 1, caplog.text
    message = shouted[0].getMessage()
    assert with_history in message and unconverged["org_id"] in message
    assert "version 2" in message

    # The screen still reads one revision per number, and the governed one.
    assert [row["version_number"] for row in history] == [4, 3, 2, 1]
    assert [row["source"] for row in history] == [
        catalogue.AUTHORITY,
        catalogue.SUPERSEDED,
        catalogue.AUTHORITY,
        catalogue.SUPERSEDED,
    ]


def test_a_clean_identity_shouts_about_nothing(live_postgres, unconverged, caplog):
    """The alarm has to be silent on a healthy base, or nobody will read it."""
    import logging

    from core import business_identity_catalogue as catalogue
    from core.business_taxonomy import list_taxonomy
    from core.master_data_convergence import converge_org_taxonomy

    with_history = str(
        list_taxonomy(live_postgres, org_id=unconverged["org_id"])["domains"][0]["id"]
    )
    _append_legacy_revisions(live_postgres, domain_id=with_history, upto=3)
    converge_org_taxonomy(
        live_postgres,
        org_id=unconverged["org_id"],
        project_id=unconverged["project_id"],
        actor=ACTOR,
        idempotency_key=f"conv_{uuid.uuid4().hex}",
        reason="AI-324",
    )

    with caplog.at_level(logging.ERROR, logger="core.business_identity_catalogue"):
        catalogue.domain_versions(
            live_postgres, org_id=unconverged["org_id"], domain_id=with_history
        )

    assert not [
        record
        for record in caplog.records
        if catalogue.VERSION_NUMBER_COLLISION_CODE in record.getMessage()
    ], caplog.text
