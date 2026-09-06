from __future__ import annotations

import re

import pytest
from core.project_settings import (
    CAPABILITY_SPECS,
    ProjectSettingsBlocked,
    ProjectSettingsStale,
    ProjectSettingsValidationError,
    canonical_hash,
    prepare_change_payload,
    validate_capability_ledger,
    validate_confirmation,
)

from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)


def _coverage(applicable: int = 0, **overrides: int) -> dict:
    counts = {
        "applicable": applicable,
        "complete": 0,
        "partial": 0,
        "unavailable": 0,
        "excluded": 0,
        "pending": 0,
    }
    counts.update(overrides)
    return counts


def test_fixed_capability_contract_is_exact() -> None:
    assert list(CAPABILITY_SPECS) == [
        "country",
        "currency_fx",
        "reporting_timezone",
        "tax_fees",
        "competitors",
        "placement_mapping",
        "analytics_alignment",
    ]
    assert CAPABILITY_SPECS["currency_fx"]["availability"] == "always_present"
    assert CAPABILITY_SPECS["reporting_timezone"]["availability"] == "always_present"
    assert CAPABILITY_SPECS["country"]["availability"] == "optional"
    # Story 61.5, ratified in `project-settings.md`: "Placement Mapping |
    # Optional and Disabled by default […] Currency & FX is required".
    assert CAPABILITY_SPECS["placement_mapping"]["availability"] == "optional"
    assert CAPABILITY_SPECS["placement_mapping"]["dependencies"] == ["currency_fx"]
    # Story 70.3, ratified in `capabilities/analytics-alignment.md`: off by
    # default, and Currency & FX is the one dependency this list can express --
    # the common key and the approved relationship are not capabilities.
    assert CAPABILITY_SPECS["analytics_alignment"]["availability"] == "optional"
    assert CAPABILITY_SPECS["analytics_alignment"]["dependencies"] == ["currency_fx"]


def test_ledger_rejects_missing_duplicate_and_inconsistent_coverage() -> None:
    rows = [
        {
            "key": key,
            "availability": spec["availability"],
            "coverage": _coverage(),
        }
        for key, spec in CAPABILITY_SPECS.items()
    ]
    validate_capability_ledger(rows)
    with pytest.raises(ProjectSettingsValidationError):
        validate_capability_ledger(rows[:-1])
    with pytest.raises(ProjectSettingsValidationError):
        validate_capability_ledger([*rows, rows[0]])
    rows[0]["coverage"] = _coverage(applicable=2, complete=1)
    with pytest.raises(ProjectSettingsValidationError):
        validate_capability_ledger(rows)


def _ledger_rows(keys=None) -> list[dict]:
    """One ledger row per capability, in the order the reader must produce them."""
    specs = CAPABILITY_SPECS if keys is None else {key: CAPABILITY_SPECS[key] for key in keys}
    return [
        {"key": key, "availability": spec["availability"], "coverage": _coverage()}
        for key, spec in specs.items()
    ]


def test_the_ledger_counts_itself_rather_than_a_number_written_in_a_sentence() -> None:
    """A full ledger validates, one row short no longer does -- and the message
    says which number, DERIVED.

    `epic-61:66` calls this "la validation de posture cesse de compter cinq". The
    comparison never counted five: it has always been `len(CAPABILITY_SPECS)`.
    Only the SENTENCE said five, and a message that contradicts the rule it
    reports is what sends a reader looking for a cardinality check that is not
    there. The assertion below is written the same way for the same reason: a
    literal here would have to be edited by every story that adds a capability,
    and the one that forgets makes this test report the wrong number.
    """
    rows = _ledger_rows()
    assert len(rows) == len(CAPABILITY_SPECS)
    validate_capability_ledger(rows)

    with pytest.raises(ProjectSettingsValidationError) as raised:
        validate_capability_ledger(rows[:-1])
    assert str(raised.value) == f"exactly {len(CAPABILITY_SPECS)} capabilities are required"


def test_the_sixth_capability_is_refused_out_of_its_declared_order() -> None:
    """The guard the `ORDER BY CASE` of `read_project_settings` has to satisfy.

    That CASE used to end in `ELSE 5`, which put `competitors` and
    `placement_mapping` at the same rank -- and two rows tied in an ORDER BY are
    not ordered. `validate_capability_ledger` compares the key list to
    `CAPABILITY_SPECS` IN ORDER, so the envelope would have raised on some reads
    and not on others: Project Settings dead by intermittence, which is worse
    than dead outright.
    """
    rows = _ledger_rows()
    # The LAST TWO, whichever they are. Swapping two rows named by index was
    # exact while six existed and silently DROPPED the seventh the day one
    # arrived -- the error became `exactly N capabilities are required`, and the
    # ordering this test exists to guard stopped being exercised at all.
    swapped = [*rows[:-2], rows[-1], rows[-2]]

    assert [row["key"] for row in swapped][-2:] == [
        list(CAPABILITY_SPECS)[-1],
        list(CAPABILITY_SPECS)[-2],
    ]
    assert len(swapped) == len(rows)
    with pytest.raises(ProjectSettingsValidationError, match="unordered"):
        validate_capability_ledger(swapped)


def test_a_posture_frozen_at_five_keys_still_prepares_and_confirms(monkeypatch) -> None:
    """`epic-61:130-133`: the migration must not block a Project created before it.

    The posture stored in an already-active Configuration Version names five
    capabilities and will never be rewritten -- those versions are immutable. A
    Change Set prepared on top of it must still compile the six, and its
    confirmation must still reach the sixth row.
    """
    import core.project_settings as settings

    frozen = {
        "defaults": {"reporting_currency": "EUR"},
        "capabilities": {
            "country": "disabled",
            "currency_fx": "enabled",
            "reporting_timezone": "enabled",
            "tax_fees": "disabled",
            "competitors": "disabled",
        },
    }

    class _FrozenCursor(_LifecycleCursor):
        def execute(self, sql, params=()):
            super().execute(sql, params)
            if "SELECT posture FROM app.project_configuration_versions" in " ".join(
                str(sql).split()
            ):
                self._row = (frozen,)

    class _FrozenConnection(_LifecycleConnection):
        def cursor(self):
            return _FrozenCursor(self)

    conn = _FrozenConnection()
    posture, _hash = settings.intended_posture(
        conn, "project_1", {"capabilities": {"placement_mapping": "enabled"}}
    )
    # The five frozen answers are kept; the sixth is added by the intent rather
    # than by a rewrite of a version that may not be rewritten.
    assert posture["capabilities"]["competitors"] == "disabled"
    assert posture["capabilities"]["placement_mapping"] == "enabled"

    payload = prepare_change_payload(
        intent={"capabilities": {"placement_mapping": "enabled"}},
        proposals=[],
        exception_references=[],
        active_configuration_version_id="pcfg_prior",
        dependency_snapshot={"datastream_set": []},
    )
    assert set(payload["impact"]["coverage"]) == set(CAPABILITY_SPECS)
    assert payload["impact"]["coverage"]["placement_mapping"]["label"] == "Not applicable"

    change = _prepared_change()
    change["intent"] = {"defaults": {}, "capabilities": {"placement_mapping": "enabled"}}
    _patch_confirmation(monkeypatch, change=change)
    confirmed = _FrozenConnection()

    result = _confirm(confirmed, change)

    assert result["outcome"] == "succeeded"
    # The confirmation reaches the SIXTH row by name. Whether that UPDATE finds a
    # row to touch is a database fact, proved against a real Postgres in
    # `test_project_settings_sixth_capability_pg.py`.
    capability_updates = [
        params
        for sql, params in confirmed.statements
        if "UPDATE app.project_capabilities" in sql
    ]
    assert [params[-1] for params in capability_updates] == ["placement_mapping"]


def test_zero_applicable_coverage_is_not_applicable_not_full() -> None:
    # No proposal compiled means no applicable Datastream. A percentage over an
    # empty denominator is the exact failure the ratified contract names.
    payload = prepare_change_payload(
        intent={"capabilities": {"country": "enabled"}},
        proposals=[],
        exception_references=[],
        active_configuration_version_id=None,
        dependency_snapshot={"datastream_set": []},
    )
    assert payload["impact"]["coverage"]["country"]["label"] == "Not applicable"
    assert payload["impact"]["coverage"]["country"]["percentage"] is None
    assert payload["impact"]["matrix"] == []


def test_prepare_is_canonical_and_confirmation_fails_closed() -> None:
    snapshot = {
        "active_configuration_version_id": "pcfg_1",
        "project_configuration": {"posture": {}},
        "datastream_set": [{"id": "ds_1", "plan_version_id": "v1"}],
        "governance_owners": [],
        "proposals": [],
    }
    kwargs = {
        "intent": {"defaults": {"reporting_currency": "USD"}},
        "proposals": [],
        "exception_references": [],
        "active_configuration_version_id": "pcfg_1",
        "dependency_snapshot": snapshot,
    }
    one = prepare_change_payload(**kwargs)
    two = prepare_change_payload(**kwargs)
    assert canonical_hash(one) == canonical_hash(two)
    validate_confirmation(one, snapshot)
    # Drift is named per dimension, never as one opaque "stale".
    with pytest.raises(ProjectSettingsStale, match="datastream_set"):
        validate_confirmation(one, {**snapshot, "datastream_set": []})
    with pytest.raises(ProjectSettingsStale, match="governance_owners"):
        validate_confirmation(
            one, {**snapshot, "governance_owners": [{"object_id": "moved"}]}
        )
    blocked = {**one, "blockers": [{"code": "missing_country_grain"}]}
    with pytest.raises(ProjectSettingsBlocked):
        validate_confirmation(blocked, snapshot)


def test_every_named_drift_dimension_invalidates_a_review() -> None:
    from core.project_settings import DRIFT_DIMENSIONS, detect_drift

    prepared = {
        "active_configuration_version_id": "pcfg_1",
        "project_configuration": {"posture": {"defaults": {}}},
        "datastream_set": [{"id": "ds_1", "source_schema_hash": "a" * 64}],
        "governance_owners": [{"object_id": "geo", "version_id": "v1"}],
        "proposals": [{"proposal_id": "dscp_1", "content_hash": "b" * 64}],
    }
    assert detect_drift(prepared, prepared) == []
    # Each dimension, one at a time: a snapshot that froze only plan and mapping
    # could be confirmed after the source schema moved underneath it.
    for dimension in DRIFT_DIMENSIONS:
        moved = {**prepared, dimension: "changed"}
        assert detect_drift(prepared, moved) == [dimension]


# EVERY STATEMENT THE CONFIRMED PATHS ISSUE, NAMED ONCE. Declaration order is
# the order an `if/elif` would test in, which is why the `FOR UPDATE` lock is
# declared before the plain read whose fragment it contains.
#
# AI-317. The chain this replaces ended in `else: self._row = None`, so five of
# the twelve statements below -- every write of the mutation -- were answered
# without ever being recognised. `None` happened to be the truth for a write,
# which is exactly the trap: the day one of those writes gains a `RETURNING`,
# or a new read joins the mutation, the fake would answer "no row" and the
# suite would stay green about a branch the product never took.
_STATEMENTS = StatementInventory(
    "test_project_settings._LifecycleCursor",
    # core/project_settings.py:1101 -- the replayed operation, read back.
    operation_replay="from app.operations",
    # core/project_settings.py:1124 -- the mutation's row lock. Declared first:
    # `project_active_version` below is this statement minus its tail.
    project_lock=(
        "select active_configuration_version_id from app.projects",
        "for update",
    ),
    # core/project_settings.py:688 -- `intended_posture`, at prepare time.
    project_active_version="select active_configuration_version_id from app.projects",
    # core/project_settings.py:1139 -- the posture of the version in force,
    # re-read inside the mutation. Keyed `(id, project_id)`.
    prior_posture=(
        "select posture from app.project_configuration_versions",
        "where id = %s and project_id = %s",
    ),
    # core/project_settings.py:444 -- `_active_posture`, the same relation read
    # by `intended_posture`, keyed the other way round `(project_id, id)`.
    active_posture=(
        "select posture from app.project_configuration_versions",
        "where project_id = %s and id = %s",
    ),
    # core/project_settings.py:1162 -- does this posture already have a version?
    version_by_content_hash="select id from app.project_configuration_versions",
    # core/project_settings.py:1171 -- the next version number.
    next_version_number="select coalesce(max(version_number), 0) + 1",
    # core/project_settings.py:1180 -- NEVER MODELLED: it only ever reached the
    # `elif` that makes it fail, and fell through the tail otherwise.
    insert_version="insert into app.project_configuration_versions",
    # core/project_settings.py:1199 -- NEVER MODELLED: the pointer move.
    activate_version="update app.projects",
    # core/project_settings.py:1234 -- NEVER MODELLED: the Project defaults.
    write_defaults="update app.project_preferences",
    # core/project_settings.py:1253 -- NEVER MODELLED: one row per capability.
    advance_capability="update app.project_capabilities",
    # core/project_settings.py:1263 -- NEVER MODELLED: the Change Set closes.
    close_change_set="update app.project_change_sets",
)


class _LifecycleCursor:
    """A cursor that answers the STATEMENT, and refuses everything else.

    AI-317. Its `execute` used to end in `else: self._row = None`. For a write
    that is the right answer by accident, and for a read it is the one answer
    psycopg reserves for "no row" -- so a query that moved, or a query added to
    the mutation tomorrow, would be measured as an absence instead of naming
    itself. `_STATEMENTS.match` now raises on anything it was not taught.
    """

    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 1
        self._row = None
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split())
        self.conn.statements.append((normalized, params))
        # A write reports no result set, and psycopg says so with `None`; every
        # SELECT below sets its description FROM the statement rather than from
        # a hand-written column list that nothing would ever read.
        self.description = None
        self._row = None
        statement = _STATEMENTS.match(sql)
        if statement == "operation_replay":
            self.description = describe(sql)
            self._row = ("succeeded", '{"active_configuration_version_id":"pcfg_2"}')
        elif statement in {"project_lock", "project_active_version"}:
            self.description = describe(sql)
            self._row = ("pcfg_prior",)
        elif statement in {"prior_posture", "active_posture"}:
            self.description = describe(sql)
            self._row = ({"defaults": {"reporting_currency": "EUR"}, "capabilities": {}},)
        elif statement == "version_by_content_hash":
            self.description = describe(sql)
            self._row = None  # no version carries this posture yet
        elif statement == "next_version_number":
            # The one projection `describe` refuses, and it says why: an
            # expression has no column name, and Postgres reports `?column?`.
            self.description = [("?column?",)]
            self._row = (2,)
        elif statement == "insert_version" and self.conn.fail_on_version_insert:
            raise RuntimeError("version insert failed")
        # Everything else in the inventory is a write with no RETURNING: no row,
        # no description. Anything NOT in the inventory raised above.

    def fetchone(self):
        return self._row


def test_the_fake_refuses_a_statement_it_was_never_taught() -> None:
    """AI-317. The tail that answered instead of refusing is gone.

    The two pairs that differ only by their tail or their key order really are
    separated -- widening either fragment would put two statements back on one
    answer, which is the failure this whole conversion exists to end.
    """
    assert (
        _STATEMENTS.find(
            "SELECT active_configuration_version_id FROM app.projects "
            "WHERE id = %s AND org_id = %s FOR UPDATE"
        )
        == "project_lock"
    )
    assert (
        _STATEMENTS.find(
            "SELECT active_configuration_version_id FROM app.projects WHERE id = %s"
        )
        == "project_active_version"
    )
    assert (
        _STATEMENTS.find(
            "SELECT posture FROM app.project_configuration_versions "
            "WHERE id = %s AND project_id = %s"
        )
        == "prior_posture"
    )
    assert (
        _STATEMENTS.find(
            "SELECT posture FROM app.project_configuration_versions "
            "WHERE project_id = %s AND id = %s"
        )
        == "active_posture"
    )

    with pytest.raises(UnknownStatement) as raised:
        _LifecycleCursor(_LifecycleConnection()).execute(
            "SELECT rollback_target FROM app.project_change_set_rollbacks WHERE id = %s",
            ("pcset_1",),
        )
    # The statement that moved, and a neighbour to compare it against.
    assert "app.project_change_set_rollbacks" in str(raised.value)
    assert "close_change_set" in str(raised.value)


class _LifecycleConnection:
    def __init__(self, *, fail_on_version_insert=False):
        self.fail_on_version_insert = fail_on_version_insert
        self.statements = []

    def cursor(self):
        return _LifecycleCursor(self)


def _prepared_change(*, blockers=None):
    snapshot = {
        "active_configuration_version_id": "pcfg_prior",
        "project_configuration": {"posture": {}},
        "datastream_set": [],
        "governance_owners": [],
        "proposals": [],
    }
    payload = {
        "diff": {"defaults": {"reporting_currency": "USD"}, "capabilities": {}},
        "dependency_snapshot": snapshot,
        "dependency_fingerprint": canonical_hash(snapshot),
        "blockers": blockers or [],
        "rollback_target": "pcfg_prior",
    }
    return {
        "id": "pcset_1",
        "state": "prepared",
        "intent": {"defaults": {"reporting_currency": "USD"}, "capabilities": {}},
        "owner_references": [],
        "prepared_payload": payload,
        "prepared_payload_hash": canonical_hash(payload),
        "dependency_fingerprint": payload["dependency_fingerprint"],
    }


def _patch_confirmation(monkeypatch, *, change, replayed=False):
    from types import SimpleNamespace

    import core.entry_confirmations as confirmations
    import core.operations as operations
    import core.project_settings as settings

    monkeypatch.setattr(settings, "_load_change_set", lambda *_args, **_kwargs: change)
    monkeypatch.setattr(
        settings,
        "_dependency_snapshot",
        lambda *_args, **_kwargs: change["prepared_payload"]["dependency_snapshot"],
    )
    monkeypatch.setattr(settings, "_load_frozen_proposals", lambda *_a, **_k: [])
    monkeypatch.setattr(settings, "_intended_hashes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        settings, "_coverage_states_by_capability", lambda *_a, **_k: {}
    )
    import core.capability_proposals as proposals

    monkeypatch.setattr(
        proposals, "persist_configuration_owner_references", lambda *_a, **_k: 0
    )
    monkeypatch.setattr(
        confirmations,
        "consume_entry_confirmation",
        lambda *_args, **_kwargs: SimpleNamespace(
            replayed=replayed,
            operation_id="op_existing" if replayed else None,
        ),
    )
    monkeypatch.setattr(confirmations, "bind_entry_confirmation_operation", lambda *_a, **_k: None)

    def execute(conn, _spec, *, mutation):
        result = mutation(conn, "op_1")
        return SimpleNamespace(
            operation_id="op_1",
            outcome=result.outcome,
            result=result.result,
            replayed=False,
        )

    monkeypatch.setattr(operations, "execute_operation", execute)


def _confirm(conn, change):
    import core.project_settings as settings

    return settings.confirm_change_set(
        conn,
        project_id="project_1",
        org_id="org_1",
        change_set_id="pcset_1",
        actor_person_id="person_1",
        confirmation_id="econf_1",
        confirmation_secret="secret",
        prepared_payload_hash=change["prepared_payload_hash"],
        idempotency_key="same-key",
    )


def test_confirmed_defaults_carry_an_origin_the_database_accepts(monkeypatch) -> None:
    """The origin written on confirmation must be one of the three earned labels.

    `app.project_preferences` constrains `canonical_currency_origin` and
    `reporting_timezone_origin` to ('operator', 'suggestion', 'default') --
    migration 152, whose comment names `core.project_provenance` as the single
    decision point. Confirmation wrote a fourth label, `project_change_set`, so
    every confirmed change set carrying a default died on the CHECK.

    The tests around this one already send `reporting_currency` and already
    record the UPDATE; none of them looked at the value, and the fake connection
    enforces no constraint. So the statement was exercised and its content never
    read -- which is how a vocabulary drift reaches production.

    A change set confirmed by a person IS the operator case: someone sent the
    value. Measured live 2026-08-07, before: 503 project_settings_unavailable
    hiding `CheckViolation ... chk_project_preferences_currency_origin`.
    """
    from core.project_provenance import VALID_ORIGINS

    change = _prepared_change()
    _patch_confirmation(monkeypatch, change=change)
    conn = _LifecycleConnection()

    _confirm(conn, change)

    preference_updates = [
        sql for sql, _ in conn.statements if "UPDATE app.project_preferences" in sql
    ]
    assert preference_updates, "confirmation no longer writes the Project defaults"
    for sql in preference_updates:
        for origin in re.findall(r"_origin = '([^']+)'", sql):
            assert origin in VALID_ORIGINS, (
                f"{origin!r} is not one of {VALID_ORIGINS}; the CHECK on "
                "app.project_preferences refuses it"
            )


def test_confirm_activates_one_new_version_and_preserves_prior_posture(monkeypatch) -> None:
    change = _prepared_change()
    _patch_confirmation(monkeypatch, change=change)
    conn = _LifecycleConnection()

    result = _confirm(conn, change)

    assert result["outcome"] == "succeeded"
    updates = [(sql, params) for sql, params in conn.statements if "UPDATE app.projects" in sql]
    assert len(updates) == 1
    assert updates[0][1][1] == "project_1"
    assert any(
        "INSERT INTO app.project_configuration_versions" in sql for sql, _ in conn.statements
    )
    assert any(
        params == ("pcfg_prior", "project_1")
        for sql, params in conn.statements
        if "SELECT posture" in sql
    )


def test_confirm_failure_never_moves_the_prior_active_pointer(monkeypatch) -> None:
    change = _prepared_change()
    _patch_confirmation(monkeypatch, change=change)
    conn = _LifecycleConnection(fail_on_version_insert=True)

    with pytest.raises(RuntimeError, match="version insert failed"):
        _confirm(conn, change)

    assert not any("UPDATE app.projects" in sql for sql, _ in conn.statements)


def test_confirm_rejects_stale_and_blocked_payloads_before_mutation(monkeypatch) -> None:
    import core.project_settings as settings

    stale = _prepared_change()
    _patch_confirmation(monkeypatch, change=stale)
    monkeypatch.setattr(settings, "_dependency_snapshot", lambda *_a, **_k: {"drifted": True})
    with pytest.raises(ProjectSettingsStale):
        _confirm(_LifecycleConnection(), stale)

    blocked = _prepared_change(blockers=[{"code": "owner_gap"}])
    _patch_confirmation(monkeypatch, change=blocked)
    with pytest.raises(ProjectSettingsBlocked):
        _confirm(_LifecycleConnection(), blocked)


def test_confirm_replay_returns_the_bound_operation_without_mutation(monkeypatch) -> None:
    change = _prepared_change()
    _patch_confirmation(monkeypatch, change=change, replayed=True)
    conn = _LifecycleConnection()

    result = _confirm(conn, change)

    assert result["idempotent_replay"] is True
    assert result["operation_id"] == "op_existing"
    assert not any("UPDATE app.projects" in sql for sql, _ in conn.statements)

def _country_proposal():
    return {
        "id": "dscp_country_1",
        "datastream_id": "ds_1",
        "capability_key": "country",
        "content_hash": "a" * 64,
        "dependency_fingerprint": "b" * 64,
        "governance_owner_references": [],
        "coverage_state": "partial",
        "applicability": "applicable",
        "impact": {
            "detected_support_selection": {
                "proposed_plan_content_hash": "c" * 64,
            }
        },
        "dependency_snapshot": {"current_plan_version_id": "dsp_1"},
        "current_plan_version_id": "dsp_1",
    }


def test_confirm_applies_country_plan_fan_out_inside_the_same_mutation(monkeypatch):
    import core.country_activation as country_activation
    import core.project_settings as settings

    change = _prepared_change()
    change["intent"]["capabilities"] = {"country": "enabled"}
    _patch_confirmation(monkeypatch, change=change)
    proposal = _country_proposal()
    monkeypatch.setattr(
        settings, "_load_frozen_proposals", lambda *_a, **_k: [proposal]
    )
    called = {}

    def fan_out(*_args, **kwargs):
        called.update(kwargs)
        return {
            "plan_versions": [
                {
                    "datastream_id": "ds_1",
                    "previous_plan_version_id": "dsp_1",
                    "plan_version_id": "dsp_country_2",
                }
            ]
        }

    monkeypatch.setattr(
        country_activation, "apply_country_plan_fan_out", fan_out, raising=False
    )

    result = _confirm(_LifecycleConnection(), change)

    assert called["change_set_id"] == "pcset_1"
    assert called["enabled"] is True
    assert result["result"]["country_plan_fan_out"]["plan_versions"][0][
        "plan_version_id"
    ] == "dsp_country_2"


def test_confirm_propagates_country_fan_out_failure_for_atomic_rollback(monkeypatch):
    import core.country_activation as country_activation
    import core.project_settings as settings

    change = _prepared_change()
    change["intent"]["capabilities"] = {"country": "enabled"}
    _patch_confirmation(monkeypatch, change=change)
    monkeypatch.setattr(
        settings, "_load_frozen_proposals", lambda *_a, **_k: [_country_proposal()]
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError("country fan-out failed")

    monkeypatch.setattr(
        country_activation, "apply_country_plan_fan_out", fail, raising=False
    )

    with pytest.raises(RuntimeError, match="country fan-out failed"):
        _confirm(_LifecycleConnection(), change)

def test_country_owner_link_pins_the_current_registry_version(monkeypatch):
    import core.country_registry as country_registry
    from core.project_settings import _owner_links

    monkeypatch.setattr(
        country_registry,
        "fetch_country_registry",
        lambda *_args, **_kwargs: {
            "id": "mdr_country_1",
            "current_version_id": "mdv_country_2",
        },
    )

    links = _owner_links(object(), "project_1", "country")

    governance = next(link for link in links if link["owner"] == "Governance")
    assert governance["owner_reference"] == {
        "surface": "project",
        "workspace": "governance",
        "section": "master-data",
        "global_surface": None,
        "global_section": None,
        "object_type": "registry",
        "object_id": "mdr_country_1",
        "tab": "versions",
        "action": None,
        "version_id": "mdv_country_2",
        "evidence_id": None,
    }


# ---------------------------------------------------------------------------
# `_coverage` doit accepter SA PROPRE SORTIE (2026-08-04)
#
# Le lecteur range `_coverage(...)` dans `capabilities[i]["coverage"]`
# (project_settings.py:1476) puis `validate_capability_ledger` revalide ce meme
# dict (:1482). Comme `_coverage` AJOUTE `label` et `percentage`, la seconde
# passe voyait huit cles la ou six etaient attendues et levait
# `coverage keys are inconsistent` -- sur TOUS les projets. Project Settings >
# General ne degradait pas : il etait mort.
#
# Les tests d'origine ne pouvaient pas l'attraper : leur helper local fabrique
# des comptes bruts, jamais la sortie de la vraie fonction.
# ---------------------------------------------------------------------------

from core.project_settings import _coverage as derive_coverage  # noqa: E402


def test_coverage_accepts_its_own_output() -> None:
    counts = {
        "applicable": 3,
        "complete": 1,
        "partial": 0,
        "unavailable": 0,
        "excluded": 0,
        "pending": 2,
    }
    once = derive_coverage(counts)
    assert once["label"] == "33.3% complete"
    # C'EST la regression : la seconde passe levait.
    assert derive_coverage(once) == once


def test_coverage_accepts_its_own_output_on_an_empty_denominator() -> None:
    """`percentage` vaut None ici -- un comparateur naif casserait dessus."""
    keys = ("applicable", "complete", "partial", "unavailable", "excluded", "pending")
    empty = dict.fromkeys(keys, 0)
    once = derive_coverage(empty)
    assert once["label"] == "Not applicable" and once["percentage"] is None
    assert derive_coverage(once) == once


def test_a_stale_label_is_refused_rather_than_believed() -> None:
    """Idempotente ne veut pas dire permissive.

    Accepter n'importe quelle paire derivee laisserait passer un libelle perime,
    qui serait AFFICHE tel quel : un chiffre qui ment est pire qu'une erreur.
    """
    once = derive_coverage(
        {
            "applicable": 3,
            "complete": 1,
            "partial": 0,
            "unavailable": 0,
            "excluded": 0,
            "pending": 2,
        }
    )
    with pytest.raises(ProjectSettingsValidationError):
        derive_coverage({**once, "label": "99% complete"})
    with pytest.raises(ProjectSettingsValidationError):
        derive_coverage({**once, "percentage": 99.0})


def test_an_unknown_coverage_key_is_still_refused() -> None:
    """La tolerance est bornee aux DEUX cles derivees, pas ouverte."""
    with pytest.raises(ProjectSettingsValidationError):
        derive_coverage(
            {
                "applicable": 0,
                "complete": 0,
                "partial": 0,
                "unavailable": 0,
                "excluded": 0,
                "pending": 0,
                "surprise": 1,
            }
        )


def test_the_ledger_validates_a_row_carrying_derived_coverage() -> None:
    """Le chemin exact du lecteur, de bout en bout."""
    rows = [
        {
            "key": key,
            "availability": spec["availability"],
            "coverage": derive_coverage(
                {
                    "applicable": 2,
                    "complete": 1,
                    "partial": 1,
                    "unavailable": 0,
                    "excluded": 0,
                    "pending": 0,
                }
            ),
        }
        for key, spec in CAPABILITY_SPECS.items()
    ]
    validate_capability_ledger(rows)
