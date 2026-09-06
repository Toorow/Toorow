"""A pin the validator accepts is a pin the key can hold (AI-338).

WHAT WAS TRUE BEFORE MIGRATION 330, MEASURED ON A DISPOSABLE POSTGRES AT 328.
`core.golden_questions._check_business_domain_pin` judges a Business Domain pin
against `business_identity_catalogue.DOMAIN_VERSION_SOURCE` -- the union of the
two ledgers that number a domain. Five foreign keys judged the same pin against
`app.mdm_business_domain_versions` alone. On the identity the product itself
mints -- a Business Domain created in Master Data, which has an authority
revision 1 and NO row in the superseded version ledger -- the validator returned
zero refusals and the INSERT then died::

    sqlstate   = 23503
    constraint = fk_golden_question_versions_domain_version
    detail     = Key is not present in table "mdm_business_domain_versions".

WHY THIS FILE IS pg-GATED AND NOT MOCKED. Every half of the defect is schema: a
foreign key, a union, a trigger and a backfill. A `MagicMock` cursor accepts the
INSERT the real key refuses, so a mocked version of this file would have been
green on the broken schema -- which is exactly how the defect survived.

WHAT EACH TEST WOULD CATCH, named so a later reader can break one on purpose:

  * `test_a_pin_on_a_natively_minted_domain_is_stored` is the reproduction. It
    goes red again the moment the authority's trigger
    (`trg_master_data_versions_register_domain`) is dropped, or its
    `node_kind = 'business_domain'` clause stops matching;
  * `test_a_legacy_pin_is_still_stored_after_the_keys_moved` goes red if the
    backfill of `app.mdm_business_domain_versions` is dropped, or if the legacy
    trigger goes -- re-pointing the keys must not RETIRE the pins that already
    resolve, which is the whole risk of moving a key;
  * `test_a_number_no_ledger_holds_is_still_refused_by_the_key` goes red if the
    key is dropped and not replaced -- a registry that answered yes to everything
    would make every other test here pass while guarding nothing;
  * `test_the_registry_is_exactly_the_union_the_validators_judge` goes red in
    BOTH directions: a missing pair means a pin the validator accepts is refused,
    a stray pair means a pin no validator would accept is held;
  * `test_the_five_keys_judge_the_registry` is the class, not the instance: it
    reads `pg_constraint`, so a sixth pin table added later against the
    superseded ledger fails here rather than in production;
  * `test_a_draft_revision_is_not_registered` goes red if `published_at IS NOT
    NULL` is relaxed in the trigger -- a draft node version is the only kind that
    can still be DELETEd, so registering one would let a key hold a pin whose
    revision then disappears.

Rolled back by the `live_postgres` fixture: nothing this file writes survives it.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

pytestmark = pytest.mark.pg_owner

ACTOR = "owner@example.com"
REGISTRY = "app.business_domain_version_registry"


def _hex64(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Scope. One organization, one project, one Semantic View version -- everything
# a Golden Question needs to exist EXCEPT the Business Domain, which is what
# each test varies.
# ---------------------------------------------------------------------------


@pytest.fixture()
def scope(live_postgres) -> dict[str, str]:
    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(f"SELECT to_regclass('{REGISTRY}')")
        if cur.fetchone()[0] is None:
            pytest.skip(
                "migration 330 is not applied to TEST_POSTGRES_DSN -- run "
                "scripts/apply_migrations.py before the pin-registry suite"
            )

    from ulid import ULID

    suffix = uuid.uuid4().hex[:10]
    org_id = f"org_{suffix}"
    project_id = f"proj_{suffix}"
    view_id = f"sv_{ULID()}"
    view_version_id = f"svv_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (org_id, f"Org {suffix}", f"org-{suffix}", ACTOR),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s,%s,%s,%s,'active',%s)",
            (project_id, org_id, f"Project {suffix}", f"project-{suffix}", ACTOR),
        )
        cur.execute(
            "INSERT INTO app.semantic_views (id, project_id, name, lifecycle_status, created_by) "
            "VALUES (%s,%s,%s,'published',%s)",
            (view_id, project_id, "pin_registry_view", ACTOR),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_view_versions
                (id, view_id, project_id, version_number, status, name, label,
                 dependency_fingerprint, content_hash, created_by)
            VALUES (%s, %s, %s, 1, 'published', %s, %s, %s, %s, %s)
            """,
            (
                view_version_id,
                view_id,
                project_id,
                "pin_registry_view",
                "Pin registry view",
                _hex64(f"dependency-{suffix}"),
                _hex64(f"content-{suffix}"),
                ACTOR,
            ),
        )
    return {
        "conn": conn,
        "org_id": org_id,
        "project_id": project_id,
        "semantic_view_id": view_id,
        "semantic_view_version_id": view_version_id,
    }


def _converge(scope: dict) -> None:
    from core.master_data_convergence import converge_org_taxonomy

    converge_org_taxonomy(
        scope["conn"],
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        actor=ACTOR,
        idempotency_key=f"conv_{uuid.uuid4().hex}",
        reason="AI-338: the keys and the validator judge one registry",
    )


def _native_domain(scope: dict, *, name: str = "Retail Media") -> str:
    """Mint a Business Domain the way the product mints one."""
    from core.master_data_commands import create_business_identity

    created = create_business_identity(
        scope["conn"],
        project_id=scope["project_id"],
        org_id=scope["org_id"],
        kind="business_domain",
        name=name,
        actor=ACTOR,
        idempotency_key=f"create_{uuid.uuid4().hex}",
        reason="a Business Domain minted in the authority",
    )
    return str(created["result"]["node_id"])


def _legacy_domain(scope: dict) -> str:
    """The first of the six domains migration 130 seeds at org creation."""
    from core.business_taxonomy import list_taxonomy

    taxonomy = list_taxonomy(scope["conn"], org_id=scope["org_id"])
    return str(taxonomy["domains"][0]["id"])


def _append_legacy_revisions(scope: dict, *, domain_id: str, upto: int) -> None:
    """The revisions a rename through the superseded writers used to leave."""
    with scope["conn"].cursor() as cur:
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


def _definition(scope: dict, *, domain_id: str, version_number: int) -> dict:
    return {
        "business_domain_id": domain_id,
        "business_domain_version_number": version_number,
        "semantic_view_id": scope["semantic_view_id"],
        "semantic_view_version_id": scope["semantic_view_version_id"],
        "semantic_view_version_role": "baseline",
        "question": "What was paid media spend last completed month, by market?",
        "time_boundary": {"as_of": "2026-07-31", "grain": "month"},
        "expected_result": [
            {"assertion_type": "value", "member_id": "sc_spend", "tolerance": None},
            {
                "assertion_type": "cardinality",
                "rows": 12,
                "tolerance": {"kind": "numeric", "absolute": 1},
            },
        ],
        "required_provenance": [
            {"link_kind": "source", "required": True},
            {"link_kind": "semantic_view", "required": True},
            {"link_kind": "citation", "required": True},
        ],
        "expected_ai_path": {
            "grammar_version": 1,
            "required_nodes": [
                {"key": "context-hub/knowledge-note/kn_EXAMPLE", "step_kind": "knowledge_read"},
                {"key": "governance/semantic-view/sv_EXAMPLE", "step_kind": "semantic_query"},
            ],
            "forbidden_nodes": [{"tool_name": "raw_sql_passthrough"}],
            "order_constraints": [
                {
                    "before": "context-hub/knowledge-note/kn_EXAMPLE",
                    "after": "governance/semantic-view/sv_EXAMPLE",
                }
            ],
        },
        "result_type": "breakdown",
        "capability_tags": ["media_spend_reporting"],
        "severity": "critical",
    }


def _pin_refusals(scope: dict, *, domain_id: str, version_number: int) -> list[str]:
    from core.golden_questions import _check_business_domain_pin

    refusals: list = []
    _check_business_domain_pin(
        scope["conn"],
        org_id=scope["org_id"],
        payload={
            "business_domain_id": domain_id,
            "business_domain_version_number": version_number,
        },
        refusals=refusals,
    )
    return [refusal.code for refusal in refusals]


def _store(scope: dict, *, domain_id: str, version_number: int) -> dict:
    """Accept the pin AND store it -- the two halves this file exists for."""
    from core.golden_questions import create_golden_question, validate_golden_question_version

    validated = validate_golden_question_version(
        scope["conn"],
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        payload=_definition(scope, domain_id=domain_id, version_number=version_number),
    )
    return create_golden_question(
        scope["conn"],
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        title="Paid media spend by market",
        owner=ACTOR,
        validated=validated,
        actor=ACTOR,
    )


def _stored_pin(scope: dict, version_id: str) -> tuple[str, int]:
    with scope["conn"].cursor() as cur:
        cur.execute(
            "SELECT business_domain_id, business_domain_version_number "
            "FROM app.golden_question_versions WHERE id = %s",
            (version_id,),
        )
        row = cur.fetchone()
    return (str(row[0]), int(row[1]))


# ---------------------------------------------------------------------------
# The defect itself.
# ---------------------------------------------------------------------------


def test_a_pin_on_a_natively_minted_domain_is_stored(scope):
    """The measured case: accepted by the validator, refused by the key."""
    _converge(scope)
    domain_id = _native_domain(scope)

    with scope["conn"].cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.mdm_business_domain_versions WHERE domain_id = %s",
            (domain_id,),
        )
        assert cur.fetchone()[0] == 0, (
            "the identity this test is about is the one the SUPERSEDED ledger "
            "does not number -- if it does, the test no longer covers the defect"
        )

    assert _pin_refusals(scope, domain_id=domain_id, version_number=1) == []
    created = _store(scope, domain_id=domain_id, version_number=1)
    assert _stored_pin(scope, created["version_id"]) == (domain_id, 1)


def test_a_legacy_pin_is_still_stored_after_the_keys_moved(scope):
    """Moving a key must not retire the pins that already resolved."""
    domain_id = _legacy_domain(scope)
    _append_legacy_revisions(scope, domain_id=domain_id, upto=3)

    assert _pin_refusals(scope, domain_id=domain_id, version_number=3) == []
    created = _store(scope, domain_id=domain_id, version_number=3)
    assert _stored_pin(scope, created["version_id"]) == (domain_id, 3)


def test_a_pin_survives_the_convergence_that_follows_it(scope):
    """A pin stored BEFORE a convergence still resolves and is still held.

    The convergence mints an authority revision above the legacy maximum
    (migration 327), so the row already stored keeps naming the pair the legacy
    ledger holds -- and the key, now judging the registry, keeps holding it.
    """
    domain_id = _legacy_domain(scope)
    _append_legacy_revisions(scope, domain_id=domain_id, upto=3)
    created = _store(scope, domain_id=domain_id, version_number=3)

    _converge(scope)

    assert _pin_refusals(scope, domain_id=domain_id, version_number=3) == []
    assert _stored_pin(scope, created["version_id"]) == (domain_id, 3)


def test_a_number_no_ledger_holds_is_still_refused_by_the_key(scope):
    """The other direction. A key that held everything would guard nothing."""
    import psycopg

    _converge(scope)
    domain_id = _native_domain(scope)

    assert _pin_refusals(scope, domain_id=domain_id, version_number=9) == [
        "unknown_business_domain_version"
    ]

    # And the KEY refuses it too, not merely the validator -- proved by going
    # around the validator entirely, which is what a second writer would do.
    with scope["conn"].cursor() as cur:
        cur.execute("SAVEPOINT before_bogus_pin")
        with pytest.raises(psycopg.errors.ForeignKeyViolation) as caught:
            cur.execute(
                """
                INSERT INTO app.observed_cohorts
                    (id, org_id, project_id, business_domain_id, business_domain_version,
                     window_start, window_end, resolved_at, member_count,
                     filter_hash, content_hash, created_by)
                VALUES (%s, %s, %s, %s, 9,
                        NOW() - INTERVAL '1 day', NOW(), NOW(), 0, %s, %s, %s)
                """,
                (
                    f"ocoh_{'0' * 26}",
                    scope["org_id"],
                    scope["project_id"],
                    domain_id,
                    _hex64("filter"),
                    _hex64("cohort"),
                    ACTOR,
                ),
            )
        cur.execute("ROLLBACK TO SAVEPOINT before_bogus_pin")
    assert caught.value.diag.constraint_name == "fk_observed_cohorts_domain_version"
    assert "business_domain_version_registry" in (caught.value.diag.message_detail or "")


def test_a_draft_revision_is_not_registered(scope):
    """`published_at IS NOT NULL` is the whole guard on the authority's side.

    A draft node version is the ONLY kind `app.protect_master_data_object_version`
    still allows to be deleted, so registering one would let a key hold a pin
    whose revision can then disappear.
    """
    from core.master_data import create_node_version, fetch_org_node, list_node_versions

    _converge(scope)
    domain_id = _native_domain(scope)
    node = fetch_org_node(scope["conn"], org_id=scope["org_id"], node_id=domain_id)
    published = list_node_versions(
        scope["conn"], org_id=scope["org_id"], node_id=domain_id
    )
    create_node_version(
        scope["conn"],
        org_id=scope["org_id"],
        registry_id=str(node["registry_id"]),
        node_id=domain_id,
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

    with scope["conn"].cursor() as cur:
        cur.execute(
            "SELECT version_number FROM app.master_data_object_versions "
            "WHERE node_id = %s AND published_at IS NULL",
            (domain_id,),
        )
        drafts = [int(row[0]) for row in cur.fetchall()]
        assert drafts, "this test needs a draft revision to exist"
        cur.execute(
            f"SELECT version_number FROM {REGISTRY} WHERE domain_id = %s "  # noqa: S608
            "ORDER BY version_number",
            (domain_id,),
        )
        registered = [int(row[0]) for row in cur.fetchall()]

    assert registered == [1], (
        "only the published revision is nameable; the draft "
        f"{drafts} must not be registered"
    )
    assert _pin_refusals(scope, domain_id=domain_id, version_number=drafts[0]) == [
        "unknown_business_domain_version"
    ]


# ---------------------------------------------------------------------------
# The registry itself, and the class of keys.
# ---------------------------------------------------------------------------


def test_the_registry_is_exactly_the_union_the_validators_judge(scope):
    """Set equality, both directions, on a base that has just been written to.

    Not a count: a count passes when one pair is swapped for another, which is
    the drift this exists to catch.
    """
    _converge(scope)
    _native_domain(scope, name="Retail Media")
    _native_domain(scope, name="Trade Marketing")

    with scope["conn"].cursor() as cur:
        cur.execute(f"SELECT domain_id, version_number FROM {REGISTRY}")  # noqa: S608
        registered = {(str(row[0]), int(row[1])) for row in cur.fetchall()}
        cur.execute(
            """
            SELECT legacy.domain_id, legacy.version_number
              FROM app.mdm_business_domain_versions legacy
            UNION
            SELECT mdv.node_id, mdv.version_number
              FROM app.master_data_object_versions mdv
              JOIN app.master_data_nodes n
                ON n.id = mdv.node_id AND n.org_id = mdv.org_id
               AND n.project_id IS NULL
             WHERE n.node_kind = 'business_domain' AND mdv.published_at IS NOT NULL
            """
        )
        numbered = {(str(row[0]), int(row[1])) for row in cur.fetchall()}

    assert registered - numbered == set(), "a registered pair no ledger numbers"
    assert numbered - registered == set(), "a numbered revision the keys cannot hold"


def test_the_five_keys_judge_the_registry(scope):
    """The class, read from the catalog rather than from the migration."""
    with scope["conn"].cursor() as cur:
        cur.execute(
            """
            SELECT rel.relname, c.conname
              FROM pg_constraint c
              JOIN pg_class rel ON rel.oid = c.conrelid
             WHERE c.contype = 'f'
               AND c.confrelid = %s::regclass
             ORDER BY 1
            """,
            (REGISTRY,),
        )
        judging = [(str(row[0]), str(row[1])) for row in cur.fetchall()]
        cur.execute(
            """
            SELECT rel.relname, c.conname
              FROM pg_constraint c
              JOIN pg_class rel ON rel.oid = c.conrelid
             WHERE c.contype = 'f'
               AND c.confrelid = 'app.mdm_business_domain_versions'::regclass
            """
        )
        still_legacy = [(str(row[0]), str(row[1])) for row in cur.fetchall()]

    assert judging == [
        ("evaluation_run_cases", "fk_evaluation_run_cases_domain_version"),
        ("feedback_annotations", "fk_feedback_annotations_domain_version"),
        ("feedback_regression_cases", "fk_feedback_regression_cases_domain"),
        ("golden_question_versions", "fk_golden_question_versions_domain_version"),
        ("observed_cohorts", "fk_observed_cohorts_domain_version"),
    ]
    assert still_legacy == [], (
        "a key judging the superseded ledger alone refuses what the validator "
        "accepts -- that is AI-338, reintroduced"
    )


def test_the_registry_carries_a_policy_and_joins_the_tenant_tree(scope):
    """Two properties a new org-scoped table must have, asserted here too.

    `test_rls_covers_every_org_scoped_table_pg` is the repository-wide ratchet;
    this names the second one it does not check -- the blocking edge onto
    `app.organizations` is what makes `core.org_purge` reach this table on its
    own during an RGPD erasure, since that module reads `pg_constraint` at call
    time rather than a hardcoded list.
    """
    with scope["conn"].cursor() as cur:
        cur.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = %s::regclass",
            (REGISTRY,),
        )
        assert cur.fetchone() == (True, True)
        cur.execute(
            """
            SELECT confrel.relname, c.confdeltype
              FROM pg_constraint c
              JOIN pg_class confrel ON confrel.oid = c.confrelid
             WHERE c.contype = 'f' AND c.conrelid = %s::regclass
            """,
            (REGISTRY,),
        )
        assert cur.fetchall() == [("organizations", "r")]
