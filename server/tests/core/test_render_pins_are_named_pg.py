"""AI-357 -- a Render pin is derived or named, and a CHECK is never a mute 500.

WHAT WAS MEASURED, 2026-09-01, on a disposable PostgreSQL at 336 migrations.
`create_render` wrote `payload.get("renderer_adapter") or ""` into a column
`ck_renders_pins_are_exact` refuses, so a payload carrying every pin the ratified
replay contract names -- Result, retained data, Visualization Spec version,
renderer and runtime build, theme, formatter, responsive profile, display state,
evidence manifest -- raised `psycopg.errors.CheckViolation` out of the service and
out of the handler. `POST /renders` answered 500 with no body a person could act
on. The neighbouring session that met it worked around it by deriving the adapter
CLIENT-SIDE from the build id.

WHY THESE TESTS NEED A REAL DATABASE. Every property below is a CHECK, a ledger
row or an `information_schema` read. A mocked cursor accepts all three happily,
which is exactly how the defect survived: the service had unit coverage and the
column it wrote an empty string into was never inserted for real by a test that
went through `create_render`.

Every test rolls back.
"""

from __future__ import annotations

import pytest

from tests.core.test_analyze_artifacts_pg import (
    Chain,
    _uid,
    _visualization_spec_version,
)

pytestmark = pytest.mark.usefixtures("live_postgres")

#: A payload complete against the ratified replay contract
#: (`visualization-and-rendering.md:446-450`) -- and carrying NO adapter, because
#: the ratified list does not name one.
_ADAPTERLESS_PINS = {
    "renderer_build_id": "table/toorow-table@1.0.0",
    "runtime_build_id": "@toorow/card-shell/viz@0.1.0+f46c5a2ca8e3",
    "theme_version": "viz-theme@1",
    "formatter_version": "viz-formatters@1",
    "responsive_profile": "console",
}


@pytest.fixture()
def chain(live_postgres):
    built = Chain(live_postgres).build()
    yield built
    live_postgres.rollback()


def _payload(chain, **overrides) -> dict:
    payload = {
        "result_id": chain.result_id,
        "result_content_hash": "b" * 64,
        "visualization_spec_version_id": _visualization_spec_version(chain),
        "display_state": {},
        "evidence_manifest": {"semantic_view_version_id": "svv_x"},
        **_ADAPTERLESS_PINS,
    }
    payload.update(overrides)
    return payload


def _register_build(chain, *, build_id: str, renderer_id: str, family: str = "table") -> None:
    """One row of the build ledger, for this transaction only.

    The ledger is a projection of the shipped registry
    (`scripts/register_renderer_builds.py`), and it is insert-once in the database
    -- so a test seeds the row it needs and rolls it back rather than reusing
    whatever a deployment happens to carry.
    """
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.renderer_runtime_builds
                (id, runtime_build, family, renderer_id, theme_version, formatter_version,
                 responsive_profiles, git_sha)
            VALUES (%s, %s, %s, %s, 'viz-theme@1', 'viz-formatters@1',
                    ARRAY['console'], %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                build_id,
                _ADAPTERLESS_PINS["runtime_build_id"],
                family,
                renderer_id,
                "0" * 40,
            ),
        )


# ---------------------------------------------------------------------------
# The derivation: the ledger knows which renderer drew the build.
# ---------------------------------------------------------------------------


def test_the_adapter_is_derived_from_the_ledger_and_not_asked_for(chain):
    """A complete payload with NO adapter is accepted, and the row carries one.

    This is the exact request that answered 500. The value stored is the ledger's
    `renderer_id` -- not a default, not the caller's word for it.
    """
    from core import analyze_artifacts as svc

    _register_build(
        chain, build_id=_ADAPTERLESS_PINS["renderer_build_id"], renderer_id="toorow-table"
    )
    created = svc.create_render(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        actor="test",
        payload=_payload(chain),
    )
    with chain.conn.cursor() as cur:
        cur.execute("SELECT renderer_adapter FROM app.renders WHERE id = %s", (created["id"],))
        assert cur.fetchone()[0] == "toorow-table"
    chain.conn.rollback()


def test_the_registry_serves_the_adapter_it_stores(chain):
    """`registered_families` hands out the pin whose absence caused the 500.

    A caller filling every pin the registry offers must end up with a payload the
    database accepts; before AI-357 the registry offered four of the five build
    properties and the fifth was the one under the CHECK.
    """
    from core import analyze_artifacts as svc

    _register_build(
        chain,
        build_id="bar/toorow-echarts-bar@1.0.0",
        renderer_id="toorow-echarts-bar",
        family="bar",
    )
    families = svc.render_contract_state(chain.conn)["registered_families"]
    served = {f["renderer_build_id"]: f for f in families}
    assert served["bar/toorow-echarts-bar@1.0.0"]["renderer_adapter"] == "toorow-echarts-bar"
    chain.conn.rollback()


def test_a_client_adapter_that_disagrees_with_the_ledger_is_refused_with_both(chain):
    """AD: a Render whose pins name another renderer is refused, never redrawn.

    (`visualization-and-rendering.md:857`.) The refusal shows BOTH identities, so a
    reader can tell which of the two is wrong.
    """
    from core import analyze_artifacts as svc

    _register_build(
        chain, build_id=_ADAPTERLESS_PINS["renderer_build_id"], renderer_id="toorow-table"
    )
    with pytest.raises(svc.ArtifactRefused) as caught:
        svc.create_render(
            chain.conn,
            org_id=chain.org_id,
            project_id=chain.project_id,
            actor="test",
            payload=_payload(chain, renderer_adapter="echarts"),
        )
    assert caught.value.code == "renderer_pin_disagrees"
    (refusal,) = caught.value.refusals
    assert refusal.subject == "renderer_adapter"
    assert "toorow-table" in refusal.message and "echarts" in refusal.message
    assert refusal.remedy
    chain.conn.rollback()


def test_the_client_adapter_is_kept_when_the_ledger_does_not_know_the_build(chain):
    """No derivation is possible, so the named pin is what freezes.

    The three integration suites that pin fictional builds go through this branch,
    and so does any deployment whose ledger has not been projected yet.
    """
    from core import analyze_artifacts as svc

    created = svc.create_render(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        actor="test",
        payload=_payload(chain, renderer_adapter="echarts"),
    )
    with chain.conn.cursor() as cur:
        cur.execute("SELECT renderer_adapter FROM app.renders WHERE id = %s", (created["id"],))
        assert cur.fetchone()[0] == "echarts"
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# The mutation: remove the derivation, and the answer is a NAMED refusal.
# ---------------------------------------------------------------------------


def test_without_the_derivation_the_answer_is_a_named_refusal_and_not_a_500(chain):
    """The mutation this repair is measured by.

    Nothing is registered for this build, so the adapter cannot be derived and the
    caller sent none. The old code wrote `''` and the CHECK raised. The refusal
    now names the FIELD and the GESTURE, and it is an `ArtifactRefused` -- which
    the route answers as 422, never as a mute 500.
    """
    from core import analyze_artifacts as svc

    with pytest.raises(svc.ArtifactRefused) as caught:
        svc.create_render(
            chain.conn,
            org_id=chain.org_id,
            project_id=chain.project_id,
            actor="test",
            payload=_payload(chain, renderer_build_id=f"table/{_uid('unregistered')}@9.9.9"),
        )
    assert caught.value.code == "incomplete_render"
    (refusal,) = caught.value.refusals
    assert refusal.code == "missing_pin"
    assert refusal.subject == "renderer_adapter"
    assert refusal.remedy and "freeze the Render" in refusal.remedy
    chain.conn.rollback()


def test_a_placeholder_adapter_is_refused_by_name(chain):
    """`is_exact_pin` refuses six words; the service says so BEFORE the database."""
    from core import analyze_artifacts as svc

    with pytest.raises(svc.ArtifactRefused) as caught:
        svc.create_render(
            chain.conn,
            org_id=chain.org_id,
            project_id=chain.project_id,
            actor="test",
            payload=_payload(chain, renderer_adapter="current"),
        )
    (refusal,) = caught.value.refusals
    assert refusal.code == "placeholder_pin"
    assert refusal.subject == "renderer_adapter"
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# The class: a CHECK the service did not anticipate is still a named refusal.
# ---------------------------------------------------------------------------


def test_a_check_the_service_cannot_pre_empt_arrives_named(chain):
    """A rule NO service branch checks: display state may carry no query.

    `ck_renders_display_state_carries_no_query` is the honest test of the class
    repair -- the service has no branch for it, so the refusal can only come from
    the translation of the constraint. Before AI-357 this was the same mute 500 as
    the adapter.
    """
    from core import analyze_artifacts as svc

    _register_build(
        chain, build_id=_ADAPTERLESS_PINS["renderer_build_id"], renderer_id="toorow-table"
    )
    with pytest.raises(svc.ArtifactRefused) as caught:
        svc.create_render(
            chain.conn,
            org_id=chain.org_id,
            project_id=chain.project_id,
            actor="test",
            payload=_payload(chain, display_state={"sql": "select 1"}),
        )
    assert caught.value.code == "display_state_carries_a_query"
    (refusal,) = caught.value.refusals
    assert refusal.subject == "display_state"
    assert refusal.remedy
    #  And the sentence names the gesture, never the constraint.
    assert "ck_renders" not in refusal.message
    chain.conn.rollback()


def test_a_report_check_is_named_too_so_the_repair_is_not_one_route(chain):
    """The same translation on another table, reached through another write.

    A Report declared as coming from a connector seed without naming the seed
    violates `ck_analysis_reports_seed_pair`. `create_report` validates the origin
    but never the pair, so the refusal can only come from the translation -- which
    is what proves the decorator is on the CLASS of writes, not on `create_render`.
    """
    from core import analyze_artifacts as svc

    with pytest.raises(svc.ArtifactRefused) as caught:
        svc.create_report(
            chain.conn,
            org_id=chain.org_id,
            project_id=chain.project_id,
            actor="test",
            label="Seeded report",
            query_spec_version_id=chain.query_spec_version_id,
            seed_origin="connector_seed",
        )
    assert caught.value.code == "incomplete_seed_origin"
    (refusal,) = caught.value.refusals
    assert refusal.subject == "seed_report_id"
    assert refusal.remedy
    assert "ck_analysis_reports" not in refusal.message
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# The enumeration: every CHECK of the domain has a translation.
# ---------------------------------------------------------------------------


@pytest.mark.pg_owner
def test_every_check_of_the_analyze_tables_has_a_translation(live_postgres):
    """The instrument that keeps the class closed.

    `information_schema` shows constraints only on tables a currently enabled role
    owns, so this test is marked `pg_owner`: run as the application role it would
    see ZERO constraints and pass while proving nothing. `untranslated_constraints`
    refuses that answer rather than returning an empty list, and the marker is what
    gives it a connection that can see the catalogue.
    """
    from core.analyze_check_refusals import (
        untranslated_constraints,
        visible_check_constraints,
    )

    visible = visible_check_constraints(live_postgres)
    #  The floor is the ten tables of the domain, each of which declares at least
    #  one rule. A number here would be a second authority that goes stale; the
    #  assertion is only that the catalogue was actually read.
    assert len(visible) >= len(
        {table for table, _ in visible}
    ), "the catalogue read returned nothing to measure"
    assert len(visible) > 10, f"only {len(visible)} CHECKs visible -- the read is not the domain"
    assert untranslated_constraints(live_postgres) == []


@pytest.mark.pg_owner
def test_the_enumeration_bites_when_a_translation_is_removed(live_postgres):
    """The mutation: drop one translation, and the enumeration names it.

    Without this, a green enumeration would prove only that the query returned
    nothing -- the failure mode the `pg_owner` marker above exists to avoid.
    """
    from core.analyze_check_refusals import CHECK_TRANSLATIONS, untranslated_constraints

    mutated = {k: v for k, v in CHECK_TRANSLATIONS.items() if k != "ck_renders_pins_are_exact"}
    missing = untranslated_constraints(live_postgres, translations=mutated)
    assert ("renders", "ck_renders_pins_are_exact") in missing


@pytest.mark.pg_owner
def test_a_role_that_cannot_see_the_catalogue_refuses_to_answer_nothing(
    live_postgres, monkeypatch
):
    """`CatalogueNotVisible`, proved rather than asserted in a docstring.

    The empty catalogue is simulated by pointing the read at a schema that holds
    none of the domain's tables -- the same shape of answer the application role
    gets, and the one an instrument must never read as "nothing is untranslated".
    """
    import core.analyze_check_refusals as mod

    monkeypatch.setattr(mod, "ANALYZE_DOMAIN_SCHEMA", "information_schema")
    with pytest.raises(mod.CatalogueNotVisible):
        mod.untranslated_constraints(live_postgres)
