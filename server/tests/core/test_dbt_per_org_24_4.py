"""Story 24.4 -- offline tests: per-org dbt orchestration + cross-project fan-out.

Offline only (no dbt, no Postgres, no DuckDB): everything is mocked. Covers
  * warehouse_tenancy.list_active_orgs -- active-org listing + degradation contract,
  * warehouse_tenancy.list_active_projects -- the zones the LIVE deployment uses
    (AI-166): with TOOROW_ORG_SCHEMAS off, `raw_<pid>` / `marts_<pid>`,
  * scheduler.run_dbt_per_project -- one dbt build per active PROJECT, isolation,
    guard, and the refusal to call a zero-model run a success (AI-166),
  * anomaly_alerts._mart_prefixes_for_scan -- flag OFF bit-identical (resolver
    NOT called) vs flag ON fan-out over active orgs (AC5, CONDITION DE FLIP),
  * cache_warehouse._mart_schema_prefixes -- same flag OFF/ON contract.

The dbt-local equivalence proof (AC4) and the seed loop (AC8) live in
server/tests/integration/test_seed_to_mart_loop.py (skipped without dbt).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from core import anomaly_alerts, cache_warehouse, scheduler
from core import warehouse_tenancy as wt


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)
    monkeypatch.delenv("DBT_NIGHTLY_ENABLED", raising=False)
    wt._reset_cache()
    yield
    wt._reset_cache()


def _org(org_id: str, slug: str) -> wt.OrgSchemas:
    built = wt._build(org_id, slug)
    assert built is not None
    return built


def _connection_patch(rows):
    """Patch core.db.get_connection to yield a cursor whose fetchall returns *rows*."""
    cur = MagicMock()
    cur.fetchall.return_value = rows
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False
    return patch("core.db.get_connection", return_value=cm)


# ---------------------------------------------------------------------------
# list_active_orgs (AC3 source-of-truth)
# ---------------------------------------------------------------------------


def test_list_active_orgs_resolves_each_active_org():
    with _connection_patch([("org_1", "acme-media"), ("org_2", "beta_co")]):
        orgs = wt.list_active_orgs()
    assert [o.warehouse_slug for o in orgs] == ["acme_media", "beta_co"]
    assert [o.marts for o in orgs] == ["org_acme_media_marts", "org_beta_co_marts"]
    assert [o.raw for o in orgs] == ["org_acme_media_raw", "org_beta_co_raw"]


def test_list_active_orgs_skips_unsanitisable_slug():
    # 'a b' has a space -> not [A-Za-z0-9_] after '-'->'_' -> skipped, never crashes.
    with _connection_patch([("org_ok", "acme"), ("org_bad", "a b")]):
        orgs = wt.list_active_orgs()
    assert [o.warehouse_slug for o in orgs] == ["acme"]


def test_list_active_orgs_degrades_to_empty_on_db_error():
    with patch("core.db.get_connection", side_effect=RuntimeError("pg down")):
        orgs = wt.list_active_orgs()
    assert orgs == []  # never raises -- nightly loop degrades to "no fan-out"


# ---------------------------------------------------------------------------
# warehouse_tenancy.list_active_projects (AI-166)
# ---------------------------------------------------------------------------


def test_list_active_projects_returns_the_zones_the_read_layer_addresses():
    """Flag OFF (the live value): `raw_<pid>` / `staging_<pid>` / `marts_<pid>`.

    These are the datasets the warehouse actually carries -- measured 2026-08-17,
    `raw_proj_01KZGCRS...` holds the landed relations while `org_toorow_raw` holds
    zero tables. The builder must address them, not the provisioned shells.
    """
    with _connection_patch([("proj_1",), ("proj_2",)]):
        zones = wt.list_active_projects()

    assert [z.project_id for z in zones] == ["proj_1", "proj_2"]
    assert [z.raw for z in zones] == ["raw_proj_1", "raw_proj_2"]
    assert [z.staging for z in zones] == ["staging_proj_1", "staging_proj_2"]
    assert [z.marts for z in zones] == ["marts_proj_1", "marts_proj_2"]
    # Not one org zone anywhere: that is the whole point of the retarget.
    assert not any("org_" in z.marts or "org_" in z.raw for z in zones)


def test_list_active_projects_follows_the_flag_when_it_is_flipped(monkeypatch):
    """Composed by the SAME resolvers the readers call, so a flip cannot split them."""
    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "1")
    with _connection_patch([("proj_1",)]):
        with patch(
            "core.warehouse_tenancy.resolve_org_schemas", return_value=_org("org_1", "acme")
        ):
            zones = wt.list_active_projects()
    assert [z.marts for z in zones] == ["org_acme_marts"]
    assert [z.raw for z in zones] == ["org_acme_raw"]


def test_list_active_projects_degrades_to_empty_on_db_error():
    with patch("core.db.get_connection", side_effect=RuntimeError("pg down")):
        assert wt.list_active_projects() == []


# ---------------------------------------------------------------------------
# scheduler.run_dbt_per_project (AI-166)
# ---------------------------------------------------------------------------


def _zones(project_id: str) -> wt.ProjectZones:
    return wt.ProjectZones(
        project_id=project_id,
        raw=f"raw_{project_id}",
        staging=f"staging_{project_id}",
        marts=f"marts_{project_id}",
    )


def _built(models: int = 3):
    """A runner whose dbt reports *models* built models."""

    def _runner(cmd, **_kw):
        lines = "".join(f"{i} of {models} OK created model m{i}\n" for i in range(models))
        return MagicMock(returncode=0, stdout=lines, stderr="")

    return _runner


def test_run_dbt_per_project_disabled_by_default():
    res = scheduler.run_dbt_per_project()
    assert res["status"] == "disabled"
    assert res["projects"] == 0


def test_run_dbt_per_project_builds_into_the_project_zones(monkeypatch):
    monkeypatch.setenv("DBT_NIGHTLY_ENABLED", "true")
    calls: list[list[str]] = []

    def _runner(cmd, **_kw):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="1 of 1 OK created model m\n", stderr="")

    res = scheduler.run_dbt_per_project(
        _runner=_runner,
        _list_projects=lambda: [_zones("proj_1"), _zones("proj_2")],
        _raw_populated=lambda dataset: True,
    )

    assert res["status"] == "ok"
    assert res["projects"] == 2 and res["ok"] == 2 and res["failed"] == 0
    assert len(calls) == 2
    # The vars name the PROJECT and its raw dataset -- never an org slug.
    assert calls[0][:3] == ["dbt", "build", "--vars"]
    assert calls[0][3] == '{"project": "proj_1", "raw_schema": "raw_proj_1"}'
    assert calls[1][3] == '{"project": "proj_2", "raw_schema": "raw_proj_2"}'
    assert all("org" not in call[3] for call in calls)
    # And the invocation says WHERE the project is, or dbt starts in /app and stops.
    assert "--project-dir" in calls[0]


def test_run_dbt_per_project_does_not_invoke_dbt_over_an_empty_raw_zone(monkeypatch):
    """A project that has landed nothing is reported as such -- never as a build."""
    monkeypatch.setenv("DBT_NIGHTLY_ENABLED", "true")
    calls: list[list[str]] = []

    def _runner(cmd, **_kw):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("core.scheduler._insert_meta_alert") as meta:
        res = scheduler.run_dbt_per_project(
            _runner=_runner,
            _list_projects=lambda: [_zones("proj_1")],
            _raw_populated=lambda dataset: False,
        )

    assert calls == []  # dbt was never spawned
    assert res["no_raw_data"] == 1 and res["ok"] == 0
    assert res["results"][0]["status"] == "no_raw_data"
    # The summary refuses the word "ok" when nothing was built.
    assert res["status"] == "nothing_built"
    # No alert: a project that has not pulled yet is an ordinary state.
    meta.assert_not_called()


def test_run_dbt_per_project_refuses_to_call_a_zero_model_run_a_success(monkeypatch):
    """Populated raw + exit 0 + zero models = the defect AI-166 names, not a green."""
    monkeypatch.setenv("DBT_NIGHTLY_ENABLED", "true")

    def _runner(cmd, **_kw):
        return MagicMock(returncode=0, stdout="Nothing to do.\n", stderr="")

    with patch("core.scheduler._insert_meta_alert") as meta:
        res = scheduler.run_dbt_per_project(
            _runner=_runner,
            _list_projects=lambda: [_zones("proj_1")],
            _raw_populated=lambda dataset: True,
        )

    assert res["ok"] == 0 and res["nothing_built"] == 1
    assert res["results"][0]["status"] == "nothing_built"
    assert res["results"][0]["exit_code"] == 0  # dbt was happy; the run still was not
    assert res["status"] == "nothing_built"
    meta.assert_called()  # raw carried relations and no mart came out: that IS a defect


def test_run_dbt_per_project_isolates_one_project_failure(monkeypatch):
    monkeypatch.setenv("DBT_NIGHTLY_ENABLED", "true")

    def _runner(cmd, **_kw):
        rc = 1 if '"project": "proj_1"' in cmd[3] else 0
        return MagicMock(returncode=rc, stdout="1 of 1 OK created model m\n", stderr="")

    with patch("core.scheduler._insert_meta_alert") as meta:
        res = scheduler.run_dbt_per_project(
            _runner=_runner,
            _list_projects=lambda: [_zones("proj_1"), _zones("proj_2")],
            _raw_populated=lambda dataset: True,
        )

    assert res["projects"] == 2 and res["ok"] == 1 and res["failed"] == 1
    statuses = {r["project_id"]: r["status"] for r in res["results"]}
    assert statuses == {"proj_1": "failed", "proj_2": "ok"}
    meta.assert_called()


def test_run_dbt_per_project_isolates_a_failing_raw_probe(monkeypatch):
    """The probe talks to BigQuery; one project's failure must not abort the rest."""
    monkeypatch.setenv("DBT_NIGHTLY_ENABLED", "true")

    def _probe(dataset):
        if dataset == "raw_proj_1":
            raise RuntimeError("bq unreachable")
        return True

    with patch("core.scheduler._insert_meta_alert"):
        res = scheduler.run_dbt_per_project(
            _runner=_built(),
            _list_projects=lambda: [_zones("proj_1"), _zones("proj_2")],
            _raw_populated=_probe,
        )

    assert res["failed"] == 1 and res["ok"] == 1


def test_run_dbt_per_project_skipped_when_no_active_project(monkeypatch):
    monkeypatch.setenv("DBT_NIGHTLY_ENABLED", "true")
    res = scheduler.run_dbt_per_project(_list_projects=lambda: [])
    assert res["status"] == "skipped" and res["projects"] == 0


def test_raw_zone_probe_is_a_no_op_outside_bigquery_mode(monkeypatch):
    """Nothing to probe on the local DuckDB loop -- dbt itself reports what it found."""
    monkeypatch.delenv("TOOROW_DB_MODE", raising=False)
    assert scheduler._raw_zone_is_populated("raw_proj_1") is True


# ---------------------------------------------------------------------------
# anomaly_alerts fan-out helper (AC5, CONDITION DE FLIP)
# ---------------------------------------------------------------------------


def test_anomaly_prefixes_flag_off_is_single_legacy_prefix(monkeypatch):
    monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)
    with patch("core.warehouse_tenancy.list_active_orgs") as lst:
        prefixes = anomaly_alerts._mart_prefixes_for_scan(None)
    assert prefixes == [wt.LEGACY_DUCKDB_MART_PREFIX]  # 'main_marts.'
    lst.assert_not_called()  # OFF path must NOT list active orgs (bit-identical)


def test_anomaly_prefixes_flag_on_fans_out_per_active_org(monkeypatch):
    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "1")
    orgs = [_org("org_1", "acme"), _org("org_2", "beta")]
    with patch("core.warehouse_tenancy.list_active_orgs", return_value=orgs):
        prefixes = anomaly_alerts._mart_prefixes_for_scan(None)
    assert prefixes == ["org_acme_marts.", "org_beta_marts."]


def test_anomaly_prefixes_flag_on_scoped_project_stays_single(monkeypatch):
    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "1")
    # A pinned project_id never fans out -- it resolves ONE prefix via mart_prefix.
    with patch("core.warehouse_tenancy.mart_prefix", return_value="org_x_marts.") as mp:
        with patch("core.warehouse_tenancy.list_active_orgs") as lst:
            prefixes = anomaly_alerts._mart_prefixes_for_scan("proj_1")
    assert prefixes == ["org_x_marts."]
    mp.assert_called_once_with("proj_1")
    lst.assert_not_called()


# ---------------------------------------------------------------------------
# cache_warehouse fan-out helper (AC5)
# ---------------------------------------------------------------------------


def test_cache_prefixes_flag_off_is_single_legacy_prefix(monkeypatch):
    monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)
    with patch("core.warehouse_tenancy.list_active_orgs") as lst:
        prefixes = cache_warehouse._mart_schema_prefixes()
    assert prefixes == [wt.LEGACY_DUCKDB_MART_PREFIX]
    lst.assert_not_called()


def test_cache_prefixes_flag_on_fans_out_per_active_org(monkeypatch):
    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "1")
    orgs = [_org("org_1", "acme"), _org("org_2", "beta")]
    with patch("core.warehouse_tenancy.list_active_orgs", return_value=orgs):
        prefixes = cache_warehouse._mart_schema_prefixes()
    assert prefixes == ["org_acme_marts.", "org_beta_marts."]


# ---------------------------------------------------------------------------
# AI-166 / AI-306 -- a failing build says WHAT dbt said (2026-08-24)
# ---------------------------------------------------------------------------


def _failed(stdout: str = "", stderr: str = ""):
    def _runner(cmd, **_kw):
        return MagicMock(returncode=1, stdout=stdout, stderr=stderr)

    return _runner


def test_a_failing_build_carries_dbts_own_words(monkeypatch):
    """`dbt exit=1` was the WHOLE of what a failing nightly said.

    Measured on the first nightly that ran to completion (2026-08-24): two of
    three projects failed and the only trace of why was an exit code. The output
    was captured all along -- `run_dbt` returned three integers and dropped it.
    """
    monkeypatch.setenv("DBT_NIGHTLY_ENABLED", "true")
    stdout = (
        "1 of 2 OK created model a\n"
        "Compilation Error in model fact_x (models/fact_x.sql)\n"
        "  Model 'model.toorow.stg_y' depends on a node named 'raw_y' which was not found\n"
    )
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        scheduler, "_insert_meta_alert", lambda step, reason: alerts.append((step, reason))
    )

    res = scheduler.run_dbt_per_project(
        _runner=_failed(stdout=stdout),
        _list_projects=lambda: [_zones("proj_1")],
        _raw_populated=lambda dataset: True,
    )

    assert res["failed"] == 1
    assert alerts, "a failed build must raise a meta alert"
    _step, reason = alerts[0]
    assert "Compilation Error in model fact_x" in reason, (
        f"the alert must name what dbt said, got: {reason}"
    )
    assert "exit=1" in reason, "the exit code stays -- it is how the failure is classified"


def test_a_crash_on_stderr_is_not_reported_as_a_silent_failure(monkeypatch):
    """A dbt that dies prints to stderr and NOTHING to stdout.

    A reader of stdout alone would report an empty cause for the loudest failure
    there is, which is how `No such file or directory: 'dbt'` cost a night in
    August.
    """
    monkeypatch.setenv("DBT_NIGHTLY_ENABLED", "true")
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        scheduler, "_insert_meta_alert", lambda step, reason: alerts.append((step, reason))
    )

    scheduler.run_dbt_per_project(
        _runner=_failed(stderr="Traceback (most recent call last):\nRuntimeError: boom\n"),
        _list_projects=lambda: [_zones("proj_1")],
        _raw_populated=lambda dataset: True,
    )

    assert "Traceback" in alerts[0][1]


def test_an_unrecognised_failure_still_arrives_with_something(monkeypatch):
    """An empty reason reads as "no reason given"; the tail is better than that."""
    monkeypatch.setenv("DBT_NIGHTLY_ENABLED", "true")
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        scheduler, "_insert_meta_alert", lambda step, reason: alerts.append((step, reason))
    )

    scheduler.run_dbt_per_project(
        _runner=_failed(stdout="something went sideways in a way nobody matched\n"),
        _list_projects=lambda: [_zones("proj_1")],
        _raw_populated=lambda dataset: True,
    )

    assert "sideways" in alerts[0][1]


def test_a_model_named_error_budget_is_not_mistaken_for_a_diagnosis():
    """The markers match dbt's vocabulary, not any line carrying the word."""
    lines = scheduler._dbt_error_lines("1 of 1 OK created model error_budget\n", "")

    assert lines == ["1 of 1 OK created model error_budget"], (
        "with no marker the tail is returned -- but it must not be labelled an error line"
    )


def test_the_captured_message_is_bounded(monkeypatch):
    """A build that fails on two hundred models must not become a dump."""
    stdout = "".join(f"Compilation Error in model m{i}\n" for i in range(200))

    lines = scheduler._dbt_error_lines(stdout, "")

    assert len(lines) == scheduler._DBT_ERROR_LINES
    assert all(len(line) <= scheduler._DBT_ERROR_LINE_CHARS for line in lines)


def test_a_named_profiles_dir_is_not_overridden_by_the_flag(monkeypatch):
    """`--profiles-dir` beats `DBT_PROFILES_DIR`, so passing it ignored the caller.

    Replaying a failing production build locally therefore ran against the DuckDB
    profile the local loop regenerates and answered in DuckDB's words -- an
    answer that looked like it was about production (2026-08-24).
    """
    monkeypatch.setenv("DBT_PROFILES_DIR", "/somewhere/else")

    args = scheduler._dbt_location_args()

    assert "--profiles-dir" not in args, (
        "a caller that named a profiles directory must not be overridden"
    )


def test_the_served_behaviour_does_not_move_when_nothing_is_named(monkeypatch):
    """The container sets no such variable: it must still be pointed at /app/dbt."""
    monkeypatch.delenv("DBT_PROFILES_DIR", raising=False)

    args = scheduler._dbt_location_args()

    if (scheduler._DBT_PROJECT_DIR / "profiles" / "profiles.yml").is_file():
        assert "--profiles-dir" in args
    assert "--project-dir" in args, "dbt refuses to start without its project dir"


def test_the_capture_carries_the_REASON_not_only_the_model_name():
    """dbt names the model on one line and says why on the next.

    The first real use of this capture (2026-08-24) named three broken models
    and not one cause: every marker line was taken, and every message line --
    which carries no marker -- was skipped.
    """
    stdout = (
        "Failure in model dim_project (models/marts/dim_project.sql)\n"
        "  Database Error in model dim_project (models/marts/dim_project.sql)\n"
        "    Not found: Table toorow:mirror.projects_dim was not found\n"
    )

    lines = scheduler._dbt_error_lines(stdout, "")

    assert any("Not found: Table" in line for line in lines), (
        f"the reason must travel with the model name, got: {lines}"
    )


def test_a_marker_line_is_not_glued_to_another_marker_line():
    """Two consecutive failures must stay two entries, not one run-on sentence."""
    stdout = (
        "Failure in model a (a.sql)\n"
        "Failure in model b (b.sql)\n"
    )

    lines = scheduler._dbt_error_lines(stdout, "")

    assert len(lines) == 2
    assert "model b" not in lines[0]


# ---------------------------------------------------------------------------
# AI-314 -- a model BUILT EMPTY for want of a source is not a mart
# ---------------------------------------------------------------------------
#
# `mirror_sync` defers its BigQuery writes (Phase B), so the `mirror` dataset
# does not exist in production and every model reading it used to FAIL the
# build. The models now guard themselves and are built EMPTY -- which means dbt
# exits 0 having built models, and the runner would have called that `ok` while
# every governed mart the Project produced is empty. These tests pin the word.


def _guarded(models: int = 3, guarded: tuple[str, ...] = ("dim_project",)):
    """A runner whose dbt built *models* models, *guarded* of them empty."""

    def _runner(cmd, **_kw):
        lines = "".join(f"{i} of {models} OK created model m{i}\n" for i in range(models))
        lines += "".join(
            f"12:00:00  TOOROW_SOURCE_ABSENT model={name} source=mirror "
            f"relations=project_preferences -- built EMPTY; the source has not "
            f"been written to this warehouse\n"
            for name in guarded
        )
        return MagicMock(returncode=0, stdout=lines, stderr="")

    return _runner


def test_guarded_models_are_read_off_the_marker_and_named():
    stdout = (
        "1 of 2 OK created model dim_project\n"
        "TOOROW_SOURCE_ABSENT model=dim_project source=mirror relations=project_preferences\n"
        "TOOROW_SOURCE_ABSENT model=fee_tax_rules_effective source=mirror relations=fee_tax_rules\n"
    )

    assert scheduler._dbt_guarded_models(stdout) == [
        "dim_project",
        "fee_tax_rules_effective",
    ]


def test_a_build_with_no_marker_names_no_guarded_model():
    """The marker is the ONLY evidence -- an empty mart is not the same fact."""
    assert scheduler._dbt_guarded_models("1 of 1 OK created model dim_project\n") == []


def test_a_project_whose_marts_were_built_empty_is_not_ok(monkeypatch):
    monkeypatch.setenv("DBT_NIGHTLY_ENABLED", "true")
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        scheduler, "_insert_meta_alert", lambda step, reason: alerts.append((step, reason))
    )

    res = scheduler.run_dbt_per_project(
        _runner=_guarded(guarded=("dim_project", "fee_tax_rules_effective")),
        _list_projects=lambda: [_zones("proj_1")],
        _raw_populated=lambda dataset: True,
    )

    assert res["ok"] == 0 and res["source_absent"] == 1
    assert res["results"][0]["status"] == "source_absent"
    assert res["results"][0]["exit_code"] == 0  # dbt was happy; the run still was not
    assert res["results"][0]["models_run"] == 3  # and models WERE built
    assert res["results"][0]["guarded_models"] == ["dim_project", "fee_tax_rules_effective"]
    # The summary word a reader takes away is neither "ok" nor "nothing_built":
    # the build ran and produced relations, and none of them is governed.
    assert res["status"] == "source_absent"
    assert alerts, "a warehouse missing a declared source must raise a meta alert"
    _step, reason = alerts[0]
    assert "dim_project" in reason, f"the alert must NAME what came out empty, got: {reason}"


def test_one_project_that_really_built_still_makes_the_night_ok(monkeypatch):
    """`ok` survives beside a guarded project -- it is the per-project word that moves."""
    monkeypatch.setenv("DBT_NIGHTLY_ENABLED", "true")

    def _runner(cmd, **_kw):
        if '"project": "proj_1"' in cmd[3]:
            return _guarded()(cmd)
        return MagicMock(returncode=0, stdout="1 of 1 OK created model m\n", stderr="")

    with patch("core.scheduler._insert_meta_alert"):
        res = scheduler.run_dbt_per_project(
            _runner=_runner,
            _list_projects=lambda: [_zones("proj_1"), _zones("proj_2")],
            _raw_populated=lambda dataset: True,
        )

    statuses = {r["project_id"]: r["status"] for r in res["results"]}
    assert statuses == {"proj_1": "source_absent", "proj_2": "ok"}
    assert res["status"] == "ok" and res["ok"] == 1 and res["source_absent"] == 1


def test_a_failing_build_is_still_failed_even_when_something_was_guarded(monkeypatch):
    """A non-zero exit outranks the guard: the failure is what must be repaired."""
    monkeypatch.setenv("DBT_NIGHTLY_ENABLED", "true")

    def _runner(cmd, **_kw):
        return MagicMock(
            returncode=1,
            stdout=(
                "TOOROW_SOURCE_ABSENT model=dim_project source=mirror relations=x\n"
                "Compilation Error in model fact_x\n"
            ),
            stderr="",
        )

    with patch("core.scheduler._insert_meta_alert"):
        res = scheduler.run_dbt_per_project(
            _runner=_runner,
            _list_projects=lambda: [_zones("proj_1")],
            _raw_populated=lambda dataset: True,
        )

    assert res["failed"] == 1 and res["source_absent"] == 0
    assert res["results"][0]["status"] == "failed"


def test_the_named_guarded_models_are_bounded(monkeypatch):
    """Twenty-one guarded models must not become a dump in an alert."""
    monkeypatch.setenv("DBT_NIGHTLY_ENABLED", "true")
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        scheduler, "_insert_meta_alert", lambda step, reason: alerts.append((step, reason))
    )

    scheduler.run_dbt_per_project(
        _runner=_guarded(guarded=tuple(f"model_{i:02d}" for i in range(21))),
        _list_projects=lambda: [_zones("proj_1")],
        _raw_populated=lambda dataset: True,
    )

    reason = alerts[0][1]
    assert "21 model(s)" in reason
    assert "(+13)" in reason, f"the tail must be counted, not printed, got: {reason}"
