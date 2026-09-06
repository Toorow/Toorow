"""Story 47.3 setup-observation contract tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from core.datastream_preconfiguration import (
    PreconfigurationValidationError,
    compile_preconfiguration,
)
from core.datastream_setup_observations import (
    ObservationConflict,
    ObservationNotFound,
    ObservationValidationError,
    load_compiler_observation,
    normalize_adapter_evidence,
    validate_discovery_request,
)

from tests.support.statement_router import StatementInventory, UnknownStatement

# The three reads `load_compiler_observation` makes, named (AI-317).
_COMPILER_OBSERVATION = StatementInventory(
    "_compiler_observation_connection.Cursor",
    observation="from app.datastream_setup_observations",
    target_revision=("select revision_number", "from app.datastream_setup_draft_revisions"),
    invalidation_causes=(
        "select invalidation_causes",
        "from app.datastream_setup_draft_revisions",
    ),
)


def test_the_observation_fakes_refuse_a_statement_they_were_never_taught() -> None:
    """AI-317: both fakes in this file had an `else` that ANSWERED.

    `_compiler_observation_connection` answered the invalidation causes to
    anything it did not recognise -- and a non-empty cause chain is what turns a
    compilation into a refusal, so an unrecognised read produced the refusal and
    the test read it as the compiler's decision. `_PinCursor` answered no rows,
    which the contract pin reads as "this fingerprint has never been recorded".
    """
    with pytest.raises(UnknownStatement) as raised:
        _COMPILER_OBSERVATION.match("SELECT id FROM app.datastream_setup_previews WHERE id=%s")
    assert "app.datastream_setup_previews" in str(raised.value)
    assert "invalidation_causes" in str(raised.value)

    with pytest.raises(UnknownStatement) as raised:
        _CONTRACT_PIN.match("SELECT id FROM app.connector_verification_runs WHERE id=%s")
    assert "app.connector_verification_runs" in str(raised.value)


def test_discovery_request_is_mode_specific_and_bounded() -> None:
    connector = validate_discovery_request(
        {
            "expected_revision": 4,
            "mode": "connector_pull",
            "discovery_kind": "connector_contract",
            "source_account_ref": "sacct_01HSAFE",
            "connector_ref": "generic",
            "connector_contract_version_ref": "ccv_01HSAFE",
            "report_ref": "daily",
        }
    )
    assert connector["source_account_ref"] == "sacct_01HSAFE"
    assert "access_ref" not in connector

    with pytest.raises(ObservationValidationError, match="irrelevant"):
        validate_discovery_request(
            {
                **connector,
                "access_ref": "bqacc_01HSAFE",
            }
        )
    with pytest.raises(ObservationValidationError, match="expected_revision"):
        validate_discovery_request({"mode": "connector_pull"})


@pytest.mark.parametrize(
    ("mode", "payload"),
    [
        (
            "external_bq",
            {
                "expected_revision": 2,
                "mode": "external_bq",
                "discovery_kind": "warehouse_schema",
                "access_ref": "bqacc_safe",
                "object_ref": "analytics.raw.events",
                "declared_writer": "Agency ETL",
                "readonly_acknowledged": True,
            },
        ),
        (
            "managed_feed",
            {
                "expected_revision": 2,
                "mode": "managed_feed",
                "discovery_kind": "file_schema",
                "channel": "file_upload",
                "staged_asset_ref": "dsa_safe",
            },
        ),
    ],
)
def test_non_connector_modes_keep_writer_and_channel_evidence(mode: str, payload: dict) -> None:
    normalized = validate_discovery_request(payload)
    assert normalized["mode"] == mode


def test_adapter_evidence_is_redacted_bounded_and_fingerprinted() -> None:
    evidence = normalize_adapter_evidence(
        {
            "adapter_ref": "external_bq.readonly.v1",
            "safe_metadata": {
                "fields": [{"name": "event_date", "type": "DATE"}],
                "location": "EU",
                "access_token": "must-not-survive",
                "sample_rows": [{"email": "person@example.com"}],
            },
            "coverage": {"schema": "available", "watermark": "unavailable"},
            "exceptions": [{"code": "watermark_unavailable"}],
        }
    )
    rendered = str(evidence).lower()
    assert "must-not-survive" not in rendered
    assert "person@example.com" not in rendered
    assert len(evidence["evidence_fingerprint"]) == 64
    assert evidence["coverage"]["watermark"] == "unavailable"


def test_client_authored_observed_metadata_is_rejected() -> None:
    inputs = {
        "operator_input": {
            "mode": "connector_pull",
            "source": {"connector_ref": "generic"},
            "observed_metadata": {"safe_metadata": {"fields": ["invented"]}},
        },
        "connector_contract": None,
        "observed_metadata": None,
        "project_configuration": None,
        "capabilities": [],
        "governance_presets": [],
    }
    with pytest.raises(PreconfigurationValidationError, match="observation_ref"):
        compile_preconfiguration(inputs)


def test_conflict_has_stable_public_code() -> None:
    assert ObservationConflict.code == "observation_revision_conflict"


def _compiler_observation_connection(
    observation_row,
    *,
    target_revision: int | None = 6,
    chain_causes: tuple = ("[]",),
):
    """Scripted stand-in for the three reads `load_compiler_observation` makes."""

    # The third read used to be the `else`, so ANY statement this fake did not
    # recognise was answered with the invalidation causes -- a chain of causes is
    # what turns a compilation into a refusal, so an unknown read produced the
    # refusal path and the test read it as the product's decision (AI-317).

    class Cursor:
        def __init__(self):
            self.results = []
            self.queries = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, args):
            self.queries.append((sql, args))
            statement = _COMPILER_OBSERVATION.match(sql)
            if statement == "observation":
                self.results = [observation_row]
            elif statement == "target_revision":
                self.results = [(target_revision,)] if target_revision is not None else []
            else:  # invalidation_causes
                self.results = [(cause,) for cause in chain_causes]

        def fetchone(self):
            return self.results[0] if self.results else None

        def fetchall(self):
            return list(self.results)

    class Connection:
        def __init__(self):
            self.cursor_instance = Cursor()

        def cursor(self):
            return self.cursor_instance

    return Connection()


def _observation_row(revision: int = 5) -> tuple:
    observed = datetime(2026, 8, 10, tzinfo=timezone.utc)
    return (
        "dso_1",
        "proj_1",
        "dsd_1",
        "dsdr_bound",
        revision,
        "managed_feed",
        "channel_contract",
        "managed_feed.channel.v1",
        None,
        "a" * 64,
        "b" * 64,
        None,
        {"fields": []},
        {},
        [],
        observed,
        None,
    )


def test_compiler_resolves_observation_still_valid_at_the_target_revision() -> None:
    """Navigation revisions minted after discovery do not orphan the evidence."""
    connection = _compiler_observation_connection(
        _observation_row(), target_revision=7, chain_causes=("[]", "[]")
    )
    resolved = load_compiler_observation(
        connection,
        project_id="proj_1",
        draft_id="dsd_1",
        revision_id="dsdr_current",
        observation_id="dso_1",
    )
    assert resolved["object_id"] == "dso_1"
    assert resolved["fingerprint"] == "b" * 64


def test_compiler_refuses_observation_a_source_change_invalidated() -> None:
    """A revision that changed `source` between attach and compile drops the evidence."""
    connection = _compiler_observation_connection(
        _observation_row(),
        target_revision=7,
        chain_causes=(
            "[]",
            json.dumps(
                [
                    {
                        "dependency": "source",
                        "cause": "source_changed",
                        "affected_sections": ["source"],
                    }
                ]
            ),
        ),
    )
    with pytest.raises(ObservationNotFound, match="Compatible observation"):
        load_compiler_observation(
            connection,
            project_id="proj_1",
            draft_id="dsd_1",
            revision_id="dsdr_current",
            observation_id="dso_1",
        )


def test_compiler_refuses_observation_bound_to_a_newer_revision() -> None:
    connection = _compiler_observation_connection(_observation_row(revision=8), target_revision=6)
    with pytest.raises(ObservationNotFound, match="Compatible observation"):
        load_compiler_observation(
            connection,
            project_id="proj_1",
            draft_id="dsd_1",
            revision_id="dsdr_past",
            observation_id="dso_1",
        )


def test_secrets_and_external_identifiers_are_never_accepted() -> None:
    with pytest.raises(ObservationValidationError, match="forbidden"):
        validate_discovery_request(
            {
                "expected_revision": 1,
                "mode": "connector_pull",
                "discovery_kind": "connector_contract",
                "source_account_ref": "sacct_safe",
                "connector_ref": "generic",
                "connector_contract_version_ref": "ccv_safe",
                "provider_account_id": "raw-provider-id",
            }
        )


def test_operator_union_rejects_mode_irrelevant_fields_and_unsafe_append() -> None:
    base = {
        "connector_contract": None,
        "observed_metadata": None,
        "project_configuration": None,
        "capabilities": [],
        "governance_presets": [],
    }
    with pytest.raises(PreconfigurationValidationError, match="Mode-irrelevant"):
        compile_preconfiguration(
            {
                **base,
                "operator_input": {"mode": "external_bq", "source": {"connector_ref": "generic"}},
            }
        )
    with pytest.raises(PreconfigurationValidationError, match="Append is unavailable"):
        compile_preconfiguration(
            {
                **base,
                "operator_input": {"mode": "managed_feed", "configure": {"write_mode": "append"}},
            }
        )


def test_connector_configuration_reuses_pure_intent_validation(monkeypatch) -> None:
    from types import SimpleNamespace

    import core.datastream_intents as intents
    from core.datastream_preconfiguration import _validate_connector_configuration

    captured = {}

    def fake_validate(intent, *, capabilities):
        captured["intent"] = intent
        captured["capabilities"] = capabilities
        return SimpleNamespace(executable=True, issues=())

    monkeypatch.setattr(intents, "validate_intent", fake_validate)
    capabilities = {
        "reports": [
            {
                "id": "daily",
                "selection_mode": "subset",
                "metrics": ["spend"],
                "dimensions": ["date"],
            }
        ]
    }
    _validate_connector_configuration(
        {
            "source": {"source_account_ref": "sacct_1", "report_ref": "daily"},
            "configure": {
                "metrics": "spend",
                "dimensions": "date",
                "grain": "date",
                "cadence_intent": "daily",
            },
        },
        capabilities,
    )
    assert captured["intent"]["source"]["selection"]["metrics"] == ["spend"]
    assert captured["intent"]["schedule"]["mode"] == "daily"
    assert captured["capabilities"] is capabilities


# ===========================================================================
# Story 57.10 -- the manifest category has to reach step 1, or the wizard shows
# a field nobody fills. The front proves its RENDERING from a fixture literal;
# these prove the THREAD: manifest on disk -> get_source_options -> the wire.
# ===========================================================================


class _OptionsCursor:
    """Scripted psycopg-shaped cursor. `description` exists because
    `data_surface._fetch_rows` reads it before zipping rows."""

    def __init__(self, script, log: list[str], columns: list[str] | None = None) -> None:
        self._script = script
        self._log = log
        self._columns = columns or []
        self._rows: list[tuple] = []
        self.description: list[tuple] = []

    def __enter__(self) -> "_OptionsCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, args: object = None) -> None:
        self._log.append(sql)
        self._rows = list(self._script(sql, args) or ())
        # NAMED COLUMNS OR NONE: `data_surface._fetch_rows` zips `description`
        # with each row under `strict=True`, so a lens answered with tuples and
        # no description raises instead of projecting. Set only when the rows
        # returned actually have this shape; the statements that fetch one value
        # keep the empty description they always had.
        self.description = (
            [(name,) for name in self._columns]
            if self._columns and self._rows and len(self._rows[0]) == len(self._columns)
            else []
        )

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _OptionsConn:
    def __init__(self, script, columns: list[str] | None = None) -> None:
        self._script = script
        self._columns = columns or []
        self.statements: list[str] = []

    def cursor(self) -> _OptionsCursor:
        return _OptionsCursor(self._script, self.statements, self._columns)


def _options_conn(pinned: list[tuple] | None = None) -> _OptionsConn:
    """No Source Account, no template, no inbound domain, and by default NO PIN.

    THE DEFAULT IS AN EMPTY CONTRACT TABLE, which is the state of a deployment
    where nobody has bound anything -- and the state the catalogue must still be
    full in. `pinned` supplies rows of
    ``(connector_id, version_id, fingerprint, created_at)`` when a test is about
    what a pin does to an entry."""

    def _script(sql: str, _args: object) -> list[tuple]:
        if "app.datastream_setup_drafts" in sql:
            return [(1,)]
        if "app.connector_contract_versions" in sql:
            return list(pinned or [])
        return []

    return _OptionsConn(_script)


def _sources_conn(rows: list[dict]) -> _OptionsConn:
    """The same scripted connection, answering the `sources` lens with rows.

    `_fetch_rows` names its columns from `cursor.description`, so a scripted
    cursor that returns tuples without one cannot exercise the projection at
    all -- which is why the lens was never covered here."""
    columns = list(rows[0]) if rows else []

    def _script(sql: str, _args: object):
        if "app.datastream_setup_drafts" in sql:
            return [(1,)]
        if "app.credential_accounts" in sql:
            return [tuple(row[name] for name in columns) for row in rows]
        return []

    return _OptionsConn(_script, columns)


def _source_row(account_id: str, label: str, connection_ref_id: str) -> dict:
    """Exactly the columns the `sources` statement selects, and no other."""
    return {
        "source_account_id": account_id,
        "label": label,
        "external_account_id": label,
        "discovered_for_connector": None,
        "connector_id": "google",
        "connection_ref_id": connection_ref_id,
        "authorization_scope": "organization",
        "authorization_kind": "google_direct",
        "available": True,
        "discovered_at": "2026-08-10T00:00:00Z",
        "last_seen_at": "2026-08-10T00:00:00Z",
        "connection_health_state": "ok",
        "health_checked_at": "2026-08-10T00:00:00Z",
        "used_by_count": 0,
        "owner_org_name": "Example Org",
    }


def test_source_options_carry_the_authorization_of_each_account() -> None:
    """The measurement that decides the SHAPE of step 1's second question.

    The ratified companion held that the question could not be asked "until the
    source-options payload carries an authorization reference per account", and
    that a screen must say so rather than pretend the account select answered
    it. Measured against a real Postgres on 2026-08-10, the payload does carry
    one: `data_surface.py` selects `cr.id AS connection_ref_id` and
    `project_source_account` publishes it as `connection_ref`. This holds that
    thread from the statement to the wire -- two consents in one Project are two
    ids, and every account names the one it belongs to.

    `authorization_ref` is deliberately asserted NOT to carry an id: it names a
    scope, a kind and an owner, and a screen that grouped on it would put two
    distinct Google consents of one organization in the same bucket."""
    accounts = _read_options(
        _sources_conn(
            [
                _source_row("sacct_a1", "sc-domain:example.com", "conn_alpha"),
                _source_row("sacct_a2", "sc-domain:shop.example.com", "conn_alpha"),
                _source_row("sacct_b1", "sc-domain:news.example.com", "conn_beta"),
            ]
        )
    )["source_accounts"]

    assert [account["connection_ref"]["id"] for account in accounts] == [
        "conn_alpha",
        "conn_alpha",
        "conn_beta",
    ]
    assert all(account["connection_ref"]["object_type"] == "connection" for account in accounts)
    assert "id" not in accounts[0]["authorization_ref"]


def _write_manifest(modules_dir, module: str, manifest: dict) -> None:
    import json as _json_module

    module_dir = modules_dir / module
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "manifest.json").write_text(_json_module.dumps(manifest), encoding="utf-8")


def _sample_manifest(name: str, **extra) -> dict:
    """A manifest complete enough to be catalogued: it declares capabilities."""
    return {
        "name": name,
        "display_name": name.replace("-", " ").title(),
        "source_capabilities": {
            "contract_version": "1",
            "field_discovery": {"mode": "static", "allowed_targets": []},
            "reports": [],
            "fields": [],
        },
        **extra,
    }


def _bindable_manifest(name: str) -> dict:
    """A manifest that also PASSES its offline contract check, so it can be bound.

    Catalogued and bindable are two different bars, deliberately: a module is
    listed on what it declares, and refused at the binding on what it cannot
    prove. This one clears both -- and it clears them because it IS a shipped
    manifest, renamed. Hand-writing one here would encode today's
    source-capabilities schema into a test that has no reason to know it, and
    the test would go red the next time the schema gains a field."""
    import json as _json_module
    from pathlib import Path

    # The real registry root, not `context_seed.DEFAULT_MODULES_DIR`: these tests
    # monkeypatch that to a tmp dir, and reading it here would depend on whether
    # the patch had been applied yet.
    source = Path(__file__).resolve().parents[2] / "modules" / "google-ads" / "manifest.json"
    manifest = _json_module.loads(source.read_text(encoding="utf-8"))
    manifest["name"] = name
    manifest["display_name"] = name.replace("-", " ").title()
    return manifest


def _read_options(conn) -> dict:
    from core.datastream_setup_observations import get_source_options

    return get_source_options(conn, project_id="prj_1", draft_id="dsd_1")


_CONTRACT_PIN = StatementInventory(
    "_PinCursor",
    next_version="coalesce(max(version_number), 0) + 1",
    insert_version="insert into app.connector_contract_versions",
    by_fingerprint=("select id, version_number", "from app.connector_contract_versions"),
)


class _PinCursor:
    """A cursor that answers the pin's three statements, and REFUSES a fourth."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []
        self._rows: list[tuple] = []

    def execute(self, sql: str, args: object = None) -> None:
        self.statements.append((sql, args))
        statement = _CONTRACT_PIN.match(sql)
        if statement == "next_version":
            self._rows = [(1,)]
        elif statement == "by_fingerprint":
            self._rows = [("ccv_pinned", 1, "f" * 64)]
        else:  # the INSERT itself -- it returns nothing
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None


def test_binding_a_connector_pins_the_contract_the_module_declares_now(
    tmp_path, monkeypatch
) -> None:
    """AI-279. The pin is WRITTEN BY THE BINDING, not pre-written per module.

    The discovery request used to require a `connector_contract_version_ref`, so
    binding was impossible until a row already existed -- which is why 39 rows
    were written into production. Sending none is now the ordinary case, and
    this is the moment the contract is recorded."""
    from core import context_seed
    from core.data_identities import connector_contract_fingerprint
    from core.datastream_setup_observations import _pin_connector_contract

    manifest = _bindable_manifest("alpha-ads")
    _write_manifest(tmp_path, "alpha-ads", manifest)
    monkeypatch.setattr(context_seed, "DEFAULT_MODULES_DIR", tmp_path)

    cur = _PinCursor()
    scope = _pin_connector_contract(cur, connector_ref="alpha-ads")

    assert scope["connector_contract_version_ref"] == "ccv_pinned"
    inserted = [sql for sql, _args in cur.statements if sql.startswith("INSERT INTO")]
    assert len(inserted) == 1
    assert "app.connector_contract_versions" in inserted[0]
    # NO INSTALLATION COLUMN (migration 250): the wrong parent is gone, not made
    # optional, so the writer cannot name it even by accident.
    assert "installation_id" not in inserted[0]
    # And what was pinned is what the module declares, byte for byte.
    args = next(args for sql, args in cur.statements if sql.startswith("INSERT INTO"))
    assert connector_contract_fingerprint(manifest) in args


def test_a_module_that_cannot_prove_its_contract_is_refused_at_the_binding(
    tmp_path, monkeypatch
) -> None:
    """Refused where it blocks something, not by vanishing from the catalogue.

    Hiding the Connector would leave an operator hunting for a product they can
    see is supported; the refusal belongs on the one action it prevents, with
    the manifest's own issues named."""
    from core import context_seed
    from core.datastream_setup_observations import (
        ObservationValidationError,
        _pin_connector_contract,
    )

    # Declares capabilities -- so it IS catalogued -- but declares no report
    # profile, so there is nothing for step 2 to offer and the offline contract
    # check refuses it.
    _write_manifest(tmp_path, "alpha-ads", _sample_manifest("alpha-ads"))
    monkeypatch.setattr(context_seed, "DEFAULT_MODULES_DIR", tmp_path)

    assert _read_options(_options_conn())["connectors"][0]["connector_ref"] == "alpha-ads"
    with pytest.raises(ObservationValidationError) as raised:
        _pin_connector_contract(_PinCursor(), connector_ref="alpha-ads")
    assert "cannot be pinned" in str(raised.value)


def test_a_pull_discovery_request_no_longer_demands_a_pinned_contract() -> None:
    """The wire stopped requiring what only a binding can produce."""
    from core.datastream_setup_observations import validate_discovery_request

    normalized = validate_discovery_request(
        {
            "expected_revision": 1,
            "mode": "connector_pull",
            "discovery_kind": "connector_contract",
            "source_account_ref": "sacct_1",
            "connector_ref": "alpha-ads",
            "report_ref": "daily",
        }
    )
    assert normalized["connector_ref"] == "alpha-ads"
    # The account and the Connector are still required: without them there is
    # nothing to pin a contract FOR.
    with pytest.raises(Exception):
        validate_discovery_request(
            {
                "expected_revision": 1,
                "mode": "connector_pull",
                "discovery_kind": "connector_contract",
                "source_account_ref": "sacct_1",
                "report_ref": "daily",
            }
        )


def test_the_catalogue_is_the_registry_and_no_row_is_needed_to_offer_a_connector(
    tmp_path, monkeypatch
) -> None:
    """AI-279. Every module is offered; an EMPTY contract table changes nothing.

    This is the whole repair. The catalogue used to be the rows of
    `app.connector_contract_versions`, so 39 rows were written into production --
    one per module, referenced by nothing -- to make this list non-empty. The
    list now comes from the registry, and the table stays empty until somebody
    actually binds a Connector."""
    from core import context_seed

    for module in ("alpha-ads", "beta-analytics", "gamma-search"):
        _write_manifest(tmp_path, module, _sample_manifest(module))
    monkeypatch.setattr(context_seed, "DEFAULT_MODULES_DIR", tmp_path)

    options = _read_options(_options_conn())["connectors"]

    assert [item["connector_ref"] for item in options] == [
        "alpha-ads",
        "beta-analytics",
        "gamma-search",
    ]
    # NO VERSION IS INVENTED, and the state says why rather than implying a fault.
    assert {item["contract_state"] for item in options} == {"unverified"}
    assert {item["contract_version_ref"] for item in options} == {""}
    assert {item["observed_at"] for item in options} == {None}


def test_a_pin_that_matches_the_module_reads_verified_and_one_that_does_not_reads_stale(
    tmp_path, monkeypatch
) -> None:
    """The day a manifest changes, SOMETHING says so -- the whole point of the pin.

    The pinned row is what a binding committed to. Compared against what the
    module declares now, it is either still true (`verified`) or behind
    (`stale`). Left uncompared, a changed manifest silently rewrote what an
    existing Datastream was built on."""
    from datetime import datetime, timezone

    from core import context_seed
    from core.data_identities import connector_contract_fingerprint

    matching = _sample_manifest("alpha-ads")
    _write_manifest(tmp_path, "alpha-ads", matching)
    _write_manifest(tmp_path, "beta-analytics", _sample_manifest("beta-analytics"))
    monkeypatch.setattr(context_seed, "DEFAULT_MODULES_DIR", tmp_path)

    created = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)
    options = {
        item["connector_ref"]: item
        for item in _read_options(
            _options_conn(
                [
                    ("alpha-ads", "ccv_1", connector_contract_fingerprint(matching), created),
                    ("beta-analytics", "ccv_2", "f" * 64, created),
                ]
            )
        )["connectors"]
    }

    assert options["alpha-ads"]["contract_state"] == "verified"
    assert options["alpha-ads"]["contract_version_ref"] == "ccv_1"
    assert options["alpha-ads"]["observed_at"] == created.isoformat()
    assert options["beta-analytics"]["contract_state"] == "stale"
    # STILL OFFERED, and still carrying the module's own fingerprint: what the
    # list shows is what the module serves today. `stale` warns about the
    # Datastreams built on the older contract, it does not refuse a new one.
    #
    # AND THAT IS WHY IT HANDS BACK NO VERSION REF. Naming one makes the binding
    # ADOPT that contract instead of pinning what the module declares now, so the
    # step offered today's bundle and the compile judged it against yesterday's:
    # `exact_bundle_required`, a refusal no gesture on the screen could repair.
    # Measured in production 2026-08-12. Empty is what a Connector never bound
    # already returns, and the binding pins what the person is looking at.
    assert options["beta-analytics"]["contract_version_ref"] == ""
    assert options["beta-analytics"]["reports"] == []


def test_source_options_carry_the_manifest_category_and_its_origin(tmp_path, monkeypatch) -> None:
    """A real manifest on disk, read through `load_registry_entry`, not a stub.

    Before 57.10 `get_source_options` never opened a manifest at all, so the
    wizard had no category to show and asked for one it could not propose."""
    from core import context_seed

    _write_manifest(
        tmp_path,
        "sample-connector",
        _sample_manifest("sample-connector", public_catalog={"category": "paid_media"}),
    )
    monkeypatch.setattr(context_seed, "DEFAULT_MODULES_DIR", tmp_path)

    option = _read_options(_options_conn())["connectors"][0]

    assert option["source_category"] == "paid_media"
    # A proposal that does not name its evidence source is not a proposal.
    assert option["source_category_origin"] == "connector_manifest"


def test_a_manifest_with_no_category_states_the_absence_on_both_fields(
    tmp_path, monkeypatch
) -> None:
    from core import context_seed

    _write_manifest(tmp_path, "sample-connector", _sample_manifest("sample-connector"))
    monkeypatch.setattr(context_seed, "DEFAULT_MODULES_DIR", tmp_path)

    option = _read_options(_options_conn())["connectors"][0]

    assert option["source_category"] is None
    # Never an origin without a value: that pair would claim a manifest said
    # something it did not.
    assert option["source_category_origin"] is None


def test_an_unreadable_manifest_never_takes_step_1_down(tmp_path, monkeypatch) -> None:
    """One broken manifest out of thirty-nine drops ITS entry, and no other.

    Two failure shapes, because they take different paths: corrupt JSON (handled
    inside `load_registry_entry`) and a registry read that raises outright."""
    from core import context_seed

    _write_manifest(tmp_path, "healthy-connector", _sample_manifest("healthy-connector"))
    broken_dir = tmp_path / "broken-connector"
    broken_dir.mkdir(parents=True)
    (broken_dir / "manifest.json").write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(context_seed, "DEFAULT_MODULES_DIR", tmp_path)

    corrupt = _read_options(_options_conn())
    assert [item["connector_ref"] for item in corrupt["connectors"]] == ["healthy-connector"]

    def _raise(*_args: object, **_kwargs: object):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(context_seed, "load_registry_entry", _raise)
    raising = _read_options(_options_conn())
    # A registry that cannot be read at all yields an empty catalogue and the
    # step says so -- never a half-list presented as the whole one.
    assert raising["connectors"] == []


def test_the_category_read_agrees_with_the_real_module_tree() -> None:
    """Against the 39 shipped manifests, not a fixture.

    Pinning one connector to one category would tie this file to a catalogue that
    legitimately changes; what must hold is that every category returned is a
    token of the closed enum, and that the read is genuinely wired to real files."""
    import json as _json_module
    from pathlib import Path

    from core.datastream_setup_observations import _connector_source_category

    schema = _json_module.loads(
        (Path(__file__).resolve().parents[2] / "core/schemas/manifest.schema.json").read_text(
            encoding="utf-8"
        )
    )
    allowed = set(schema["properties"]["public_catalog"]["properties"]["category"]["enum"])
    assert len(allowed) == 8

    modules = sorted(
        path.parent.name
        for path in (Path(__file__).resolve().parents[2] / "modules").glob("*/manifest.json")
    )
    assert modules, "no connector manifest found: the read under test cannot be exercised"

    read = {module: _connector_source_category(module) for module in modules}
    for module, value in read.items():
        if value["source_category"] is None:
            assert value["source_category_origin"] is None, module
            continue
        assert value["source_category"] in allowed, module
        assert value["source_category_origin"] == "connector_manifest", module
    assert any(value["source_category"] for value in read.values())


def _bq_account(source_account_id: str) -> dict:
    """Shaped like `project_source_account` returns, `connection_ref` included.

    It was not, and that absence was the hole of story 57.11: these fixtures were
    the only thing describing an external-access account to a test, so nothing
    would have noticed the authorization reference disappearing from the wire.
    """
    return {
        "object_ref": {"id": source_account_id},
        "connector_ref": {"id": "bigquery"},
        "connection_ref": {"object_type": "connection", "id": f"conn_for_{source_account_id}"},
        "label": source_account_id,
        "states": {"availability": "available"},
    }


def test_a_bigquery_access_names_the_object_it_scopes() -> None:
    """Story 57.1 -- the access already IS the table, so the step reads it.

    For this Connector only a LEAF of the discovery walk carries an id, so a
    Source Account's scope name is exactly the `project.dataset.table` reference
    the wizard used to ask an operator to retype.
    """
    from core.datastream_setup_observations import _external_access_options

    accounts = [_bq_account("sacct_1"), {**_bq_account("sacct_2"), "connector_ref": {"id": "gsc"}}]
    rows = [
        {"external_account_id": "warehouse.billing.export_v1"},
        {"external_account_id": "sc-domain:one"},
    ]
    options = _external_access_options(accounts, rows)
    # The Search Console scope is not a BigQuery access and never appears here.
    assert [option["object_ref"]["id"] for option in options] == ["sacct_1"]
    assert options[0]["external_object_ref"] == "warehouse.billing.export_v1"


def test_a_capped_dataset_listing_says_it_is_capped(monkeypatch) -> None:
    """A BOUNDED LIST THAT DOES NOT SAY SO IS A FABRICATED COMPLETENESS.

    The discovery walk stops at `MAX_DISCOVERY_TABLES_PER_DATASET` objects per
    dataset and marks the dataset `truncated` -- a flag no column keeps, because
    only the leaf becomes a Source Account row. Rebuilt by counting: a dataset
    whose exposed objects reach the bound was cut, and the screen must reopen
    free entry rather than hide the rest behind a read-only prefill.
    """
    import core.datastream_setup_observations as observations

    monkeypatch.setattr(observations, "_bigquery_listing_bound", lambda: 2)
    accounts = [_bq_account("sacct_1"), _bq_account("sacct_2"), _bq_account("sacct_3")]
    rows = [
        {"external_account_id": "warehouse.capped.a"},
        {"external_account_id": "warehouse.capped.b"},
        {"external_account_id": "warehouse.small.only"},
    ]
    options = observations._external_access_options(accounts, rows)
    capped = [
        option
        for option in options
        if option["external_object_ref"].startswith("warehouse.capped")
    ]
    assert all(option["truncated"] is True for option in capped)
    assert all(option["listed_objects"] == 2 for option in capped)
    small = next(o for o in options if o["external_object_ref"] == "warehouse.small.only")
    assert small["truncated"] is False


def test_an_unknown_listing_bound_never_promises_a_complete_list(monkeypatch) -> None:
    """No bound read -> every access reads as capped. The safe direction is the
    one that reopens the field, never the one that promises completeness."""
    import core.datastream_setup_observations as observations

    monkeypatch.setattr(observations, "_bigquery_listing_bound", lambda: 0)
    options = observations._external_access_options(
        [_bq_account("sacct_1")], [{"external_account_id": "warehouse.billing.export_v1"}]
    )
    assert options[0]["truncated"] is True


def test_a_scope_that_is_not_a_three_part_reference_is_not_prefilled(monkeypatch) -> None:
    """An access that names no object leaves the reference typed, and says so
    through a null rather than a guessed string."""
    import core.datastream_setup_observations as observations

    monkeypatch.setattr(observations, "_bigquery_listing_bound", lambda: 500)
    options = observations._external_access_options(
        [_bq_account("sacct_1")], [{"external_account_id": "warehouse.billing"}]
    )
    assert options[0]["external_object_ref"] is None
    assert options[0]["truncated"] is True


def test_every_external_access_option_names_the_authorization_to_browse(monkeypatch) -> None:
    """Story 57.11 -- the browse door takes an authorization id, and this is it.

    The External BigQuery path walks `GET /api/connections/{id}/accounts` to show
    project / dataset / table live (57.1 D2: that door is reused, no second one is
    opened). The `{id}` is the `connection_ref.id` of the access being chosen, so
    an option that does not carry it cannot be browsed from -- the field falls
    back to typing a three-part reference by hand.

    Pinned HERE as well as on the account projection because the class has two
    sites: `_external_access_options` rebuilds each option and could drop the key
    while `source_accounts` kept it, and only one of the two feeds this mode.
    """
    import core.datastream_setup_observations as observations

    monkeypatch.setattr(observations, "_bigquery_listing_bound", lambda: 500)
    options = observations._external_access_options(
        [_bq_account("sacct_1"), _bq_account("sacct_2")],
        [
            {"external_account_id": "warehouse.billing.export_v1"},
            {"external_account_id": "warehouse.billing.export_v2"},
        ],
    )

    assert [option["connection_ref"]["id"] for option in options] == [
        "conn_for_sacct_1",
        "conn_for_sacct_2",
    ]
    assert all(option["connection_ref"]["id"] for option in options)


def _wide_evidence(count: int, description_length: int = 0) -> dict:
    """One observation for a table of `count` columns, shaped like a real one."""
    return {
        "adapter_ref": "external_bq.readonly.v1",
        "safe_metadata": {
            "fields": [
                {
                    "name": f"column_{index}",
                    "field_id": f"column_{index}",
                    "type": "STRING",
                    "nullable": True,
                    "mode": "NULLABLE",
                    "description": "d" * description_length,
                }
                for index in range(count)
            ],
            "quota_cost": {"bytes_scanned_estimate": 4096, "unit": "bytes"},
            "schema_hash": "a" * 64,
            "location": "EU",
            "watermark": "column_0",
        },
        "coverage": {"schema": "available", "scan_estimate": "available"},
        "exceptions": [],
    }


def test_a_wide_table_keeps_its_estimate_and_its_columns() -> None:
    """THE TRIMMER USED TO EMPTY `safe_metadata` WHOLE, and 57.1 made that fatal.

    Measured 2026-08-05 before this test existed: 60 columns carrying a 40-char
    description left SIX keys out of eighteen -- neither `fields` nor
    `quota_cost`. Two consequences, both silent: an object with sixty columns
    rendered as "this authorization exposes no readable column", a fabricated
    emptiness; and since `quota_cost` gates the preview with a 422, a wide table
    became a dead end whose stated repair -- re-run discovery -- could never
    work, because the second pass lost the same key.
    """
    for count, description in ((60, 40), (200, 0), (500, 0), (200, 40)):
        evidence = normalize_adapter_evidence(_wide_evidence(count, description))
        safe = evidence["safe_metadata"]
        expected_cost = {"bytes_scanned_estimate": 4096, "unit": "bytes"}
        assert safe["quota_cost"] == expected_cost, (count, description)
        assert safe["schema_hash"] == "a" * 64, (count, description)
        assert safe["location"] == "EU", (count, description)
        assert safe["fields"], (count, description)


def test_a_shortened_field_list_says_how_short_it_is() -> None:
    """A list that stays silent about its bound reads as the whole schema.

    Same rule as the object listing of story 57.1: the counts travel, so the
    screen can say `N of M`, and an operator does not conclude their columns do
    not exist.
    """
    evidence = normalize_adapter_evidence(_wide_evidence(500))
    listed = int(evidence["coverage"]["fields_listed"])
    assert evidence["coverage"]["fields_observed"] == "500"
    assert evidence["coverage"]["field_list"] == "truncated"
    assert 0 < listed < 500
    assert listed == len(evidence["safe_metadata"]["fields"])
    assert {"code": "field_list_truncated"} in evidence["exceptions"]


def test_a_list_that_fits_is_reported_complete() -> None:
    evidence = normalize_adapter_evidence(_wide_evidence(12))
    assert evidence["coverage"] == {
        "schema": "available",
        "scan_estimate": "available",
        "fields_observed": "12",
        "fields_listed": "12",
        "field_list": "complete",
    }
    assert evidence["exceptions"] == []


def test_a_folder_is_described_but_never_offered_as_a_column() -> None:
    """DESCRIBING IS NOT OFFERING, and the compiler is where the second half lives.

    Discovery emits a `REPEATED RECORD` so an operator can see it. The field
    universe refuses it, because its classification reads a physical type and
    `record` is neither a date nor a numeric -- it would become a selectable
    dimension, a grouping offered as a column.
    """
    from core.datastream_preconfiguration import _normalized_field_universe
    from core.datastream_setup_observations import is_container_field

    observed = {
        "safe_metadata": {
            "fields": [
                {"field_id": "cost", "type": "FLOAT", "mode": "NULLABLE"},
                {"field_id": "service", "type": "RECORD", "mode": "NULLABLE"},
                {"field_id": "service.description", "type": "STRING", "mode": "NULLABLE"},
                {"field_id": "credits", "type": "RECORD", "mode": "REPEATED"},
            ]
        }
    }
    assert is_container_field({"type": "RECORD", "mode": "NULLABLE"}) is True
    assert is_container_field({"type": "STRING", "mode": "REPEATED"}) is True
    assert is_container_field({"type": "STRING", "mode": "NULLABLE"}) is False

    universe = _normalized_field_universe({"observed_metadata": observed})
    offered = {item["field_id"] for item in universe}
    assert offered == {"cost", "service.description"}


def test_observing_a_first_delivery_does_not_demote_a_materialized_draft():
    """La premiere livraison s'observe APRES la materialisation, par construction.

    Une adresse entrante ne s'emet que contre un Datastream materialise, et
    `observe_first_delivery` exige un Datastream qui a deja recu : quand cette
    observation devient possible, le brouillon est DEJA `materialized`. Or
    `create_observation` forcait `state='draft'` sans condition, ce qui viole
    `datastream_setup_drafts_check` -- l'invariant
    `state='materialized' <=> materialized_datastream_id IS NOT NULL`.

    Mesure vivante 2026-08-08, sur le flux qui venait de recevoir son premier
    fichier reel : POST .../observations -> 503, et dans le journal
    `CheckViolation: new row for relation "datastream_setup_drafts" violates
    check constraint "datastream_setup_drafts_check"`. La decouverte des colonnes
    -- le pas qui suit une premiere livraison -- etait donc impossible.

    Un brouillon materialise garde son etat ; seuls ses pointeurs de revision et
    de proposition bougent. La regression se lit sur l'instruction elle-meme,
    parce que la contrainte qui la punit vit dans Postgres et pas ici.

    RENFORCE LE 2026-08-31 (AI-336). La reparation de 2026-08-08 etait une clause
    conditionnelle -- `state=CASE WHEN materialized_datastream_id IS NULL THEN
    'draft' ELSE state END` -- qui protegeait bien le brouillon materialise. Mais
    l'abandon (`archived`) est devenu un second etat qu'un brouillon peut porter,
    et cette clause l'aurait RESSUSCITE a la premiere decouverte. L'instruction
    ne touche donc plus `state` du tout, ce qui tient les deux invariants au lieu
    d'un : cette assertion est strictement plus forte que celle qu'elle remplace.
    """
    from pathlib import Path

    source = Path(
        __file__
    ).resolve().parents[2] / "core" / "datastream_setup_observations.py"
    text = source.read_text(encoding="utf-8")

    update = next(
        line
        for line in text.splitlines()
        if "UPDATE app.datastream_setup_drafts" in line
    )
    index = text.splitlines().index(update)
    statement = "\n".join(text.splitlines()[index : index + 4])

    assert "state='draft'," not in statement, (
        "l'observation remet le brouillon en 'draft' sans condition : sur un "
        "brouillon materialise cela viole datastream_setup_drafts_check"
    )
    assert "state=" not in statement, (
        "l'observation touche encore `state` : un brouillon materialise doit "
        "garder le sien, et un brouillon ABANDONNE ne doit pas revenir a la vie "
        "a la premiere decouverte (AI-336)"
    )
    assert "current_revision_id=" in statement, (
        "l'instruction lue n'est plus celle qui deplace le pointeur de revision : "
        "la garde ci-dessus ne mesure plus rien"
    )
