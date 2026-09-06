"""Offline proofs for the first-candidate door (AI-142).

No database, no network, no MagicMock where the point is that a call did NOT
happen: a MagicMock answers everything, so "it was never called" is exactly the
assertion it cannot make honestly. Every double below is written by hand and
records what it received.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEAM = _REPO_ROOT / "server" / "core" / "datastream_first_candidate.py"
_MCP = _REPO_ROOT / "server" / "core" / "datastream_first_candidate_mcp.py"


# ---------------------------------------------------------------------------
# Hand-written doubles.
# ---------------------------------------------------------------------------


class _Cursor:
    """A cursor that records every statement and replays queued rows."""

    def __init__(self, rows: list, log: list):
        self._rows = list(rows)
        self._log = log
        self.description = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._log.append((" ".join(str(sql).split()), params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class _Conn:
    """A connection double: no transaction semantics, only observability."""

    def __init__(self, rows=()):
        self.statements: list = []
        self._rows = list(rows)
        self.transactions = 0

    def cursor(self):
        return _Cursor(self._rows, self.statements)

    def transaction(self):
        conn = self

        class _Txn:
            def __enter__(self):
                conn.transactions += 1
                return conn

            def __exit__(self, *exc):
                return False

        return _Txn()


class _Never:
    """A callable that fails the test if it is ever called, by name."""

    def __init__(self, name: str):
        self.name = name
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError(f"{self.name} must not be called")


class _Recorder:
    """A callable that records its keyword arguments and returns a fixed value."""

    def __init__(self, returns=None):
        self.calls: list[tuple[tuple, dict]] = []
        self._returns = returns

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self._returns


def _armed_facts(**overrides):
    facts = {
        "project_id": "proj_EXAMPLE",
        "datastream_id": "ds_EXAMPLE",
        "org_id": "org_EXAMPLE",
        "name": "Example stream",
        "lifecycle_state": "draft",
        "enabled": True,
        "schedule_mode": "manual",
        "source_kind": "connector_pull",
        "module_name": "example",
        "connection_ref_id": "cref_EXAMPLE",
        "report_profile_id": "rep_EXAMPLE",
        "current_plan_version_id": "dspv_EXAMPLE",
        "current_mapping_version_id": "dsmv_EXAMPLE",
        "current_published_execution_id": None,
        "mapping_executable": True,
        "mode": "connector_pull",
        "mode_source": "declared",
        "window_days": 7,
        "has_schedule_state": True,
        "active_execution_id": None,
        "ready_candidate_execution_id": None,
        "pending_candidate_execution_id": None,
        "projection_executable": True,
        "projection_issue_codes": [],
    }
    facts.update(overrides)
    facts["blockers"] = []
    return facts


# ---------------------------------------------------------------------------
# Pure invariants.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_kind", "module_name", "expected_mode", "expected_source"),
    [
        ("connector_pull", "example", "connector_pull", "declared"),
        ("external_bq", None, "external_bq", "declared"),
        ("managed_feed", None, "managed_feed", "declared"),
        (None, "example", "connector_pull", "derived_from_module_name"),
        ("", "example", "connector_pull", "derived_from_module_name"),
        (None, None, None, "undeclared"),
        ("something_else", "example", None, "undeclared"),
    ],
)
def test_mode_resolution_is_declared_or_visibly_derived_or_refused(
    source_kind, module_name, expected_mode, expected_source
) -> None:
    from core.datastream_first_candidate import resolve_mode

    assert resolve_mode(source_kind, module_name) == (expected_mode, expected_source)


@pytest.mark.parametrize(
    ("window", "refetch", "expected"),
    [
        (7, 3, 7),  # date_window_days wins
        (None, 5, 5),  # legacy fallback
        (0, 0, 3),  # floor
        (None, None, 3),
        (9999, None, 365),  # ceiling
        ("nonsense", 4, 4),
    ],
)
def test_effective_window_follows_the_schedule_surface_precedence(window, refetch, expected):
    from core.datastream_first_candidate import effective_window_days

    assert effective_window_days(window, refetch) == expected


def test_first_window_ends_yesterday_and_spans_exactly_the_window() -> None:
    from core.datastream_first_candidate import bounded_interval

    interval = bounded_interval(7, today=date(2026, 8, 2))
    assert interval == {"from": "2026-07-26", "to": "2026-08-01"}
    span = date.fromisoformat(interval["to"]) - date.fromisoformat(interval["from"])
    assert span.days + 1 == 7


def test_arming_reports_every_blocker_not_only_the_first() -> None:
    from core.datastream_first_candidate import evaluate_arming

    blockers = evaluate_arming(
        {
            "current_plan_version_id": None,
            "current_mapping_version_id": None,
            "mode": None,
            "active_execution_id": "dse_EXAMPLE",
        }
    )
    codes = [item["code"] for item in blockers]
    assert codes == [
        "no_plan_version",
        "no_mapping_version",
        "undeclared_source_kind",
        "execution_in_flight",
    ]


def test_a_fully_armed_datastream_has_no_blockers() -> None:
    from core.datastream_first_candidate import evaluate_arming

    assert evaluate_arming(_armed_facts()) == []


def test_connector_pull_without_a_connection_is_refused() -> None:
    from core.datastream_first_candidate import evaluate_arming

    codes = [b["code"] for b in evaluate_arming(_armed_facts(connection_ref_id=None))]
    assert codes == ["no_connection_binding"]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "start_datastream_first_candidate"),
        (
            {"ready_candidate_execution_id": "dse_EXAMPLE"},
            "publish_activate_datastream_candidate with that execution_id",
        ),
        (
            {"pending_candidate_execution_id": "dse_EXAMPLE"},
            "wait: a candidate is materializing; its adapter has not returned yet",
        ),
    ],
)
def test_next_step_names_one_action(overrides, expected) -> None:
    from core.datastream_first_candidate import next_step

    assert next_step(_armed_facts(**overrides)) == expected


def test_next_step_names_the_repair_when_blocked() -> None:
    from core.datastream_first_candidate import next_step

    facts = _armed_facts()
    facts["blockers"] = [{"code": "no_plan_version", "detail": "x"}]
    assert next_step(facts) == "repair: no_plan_version"


# ---------------------------------------------------------------------------
# The dispatch writes a candidate and a job -- and nothing else.
# ---------------------------------------------------------------------------


def test_dispatch_pins_current_versions_and_carries_mode_and_window_in_the_payload(
    monkeypatch,
) -> None:
    import core.datastream_publication as publication
    import core.queue as queue
    from core.datastream_first_candidate import first_candidate_mutation

    created = _Recorder(returns={"id": "dse_NEW", "state": "created"})
    enqueued = _Recorder(returns={"job_id": "dsaj_NEW", "state": "queued", "replayed": False})
    monkeypatch.setattr(publication, "create_execution", created)
    monkeypatch.setattr(queue, "enqueue_activation_work", enqueued)

    projection = {"executable": True, "grain": ["date"], "issues": []}
    interval = {"from": "2026-07-26", "to": "2026-08-01"}
    result = first_candidate_mutation(
        _Conn(),
        operation_id="op_EXAMPLE",
        facts=_armed_facts(),
        projection=projection,
        interval=interval,
        actor="owner@example.com",
    )

    args, _ = created.calls[0]
    assert args[0] == "ds_EXAMPLE"
    assert args[2] == "dspv_EXAMPLE"  # the CURRENT plan version, not a new one
    assert args[3] == "dsmv_EXAMPLE"  # the CURRENT mapping version
    # The projection plan reaches `create_execution` schema-clean: the interval is
    # NOT smuggled into it (the plan schema is additionalProperties:false).
    # Story 63.7 adds exactly ONE key, `origin`, and it is DECLARED in that
    # schema -- so the plan stays re-validatable, and the candidate says which
    # path created it instead of arriving on the Runs tab unaccounted for.
    from core import run_origins

    assert args[4] == {**projection, "origin": run_origins.FIRST_CANDIDATE}
    assert "interval" not in args[4]
    # And the caller's own dict is untouched: `stamp_origin` copies.
    assert "origin" not in projection

    payload = enqueued.calls[0][1]["payload"]
    assert payload == {"mode": "connector_pull", "channel": None, "interval": interval}
    assert enqueued.calls[0][1]["kind"] == "candidate_materialization"
    assert enqueued.calls[0][1]["execution_id"] == "dse_NEW"

    assert result.outcome == "succeeded"
    assert result.result["candidate_execution_id"] == "dse_NEW"
    assert result.result["lifecycle_state_unchanged"] == "draft"


def test_dispatch_reports_failed_without_a_job_when_the_lock_is_held(monkeypatch) -> None:
    import core.datastream_publication as publication
    import core.queue as queue
    from core.datastream_first_candidate import first_candidate_mutation

    def _busy(*args, **kwargs):
        raise publication.ConcurrentExecutionActive("dse_OTHER")

    never_enqueued = _Never("enqueue_activation_work")
    monkeypatch.setattr(publication, "create_execution", _busy)
    monkeypatch.setattr(queue, "enqueue_activation_work", never_enqueued)

    result = first_candidate_mutation(
        _Conn(),
        operation_id="op_EXAMPLE",
        facts=_armed_facts(),
        projection={"executable": True},
        interval={"from": "2026-07-26", "to": "2026-08-01"},
        actor="owner@example.com",
    )
    assert result.outcome == "failed"
    assert result.result == {"reason": "execution_in_flight"}
    assert never_enqueued.calls == 0


def test_a_blocked_datastream_never_reaches_a_durable_operation(monkeypatch) -> None:
    import core.datastream_first_candidate as seam
    import core.operations as operations

    blocked = _armed_facts()
    blocked["blockers"] = [{"code": "no_connector_binding", "detail": "x"}]
    monkeypatch.setattr(seam, "read_arming", lambda *a, **k: blocked)
    never = _Never("execute_operation")
    monkeypatch.setattr(operations, "execute_operation", never)

    with pytest.raises(seam.FirstCandidateRefused) as excinfo:
        seam.dispatch_first_candidate(
            _Conn(),
            project_id="proj_EXAMPLE",
            datastream_id="ds_EXAMPLE",
            actor="owner@example.com",
            today=date(2026, 8, 2),
            idempotency_key="k",
        )
    assert "no_connector_binding" in str(excinfo.value)
    assert never.calls == 0


def test_an_unknown_datastream_never_reaches_a_durable_operation(monkeypatch) -> None:
    import core.datastream_first_candidate as seam
    import core.operations as operations

    monkeypatch.setattr(seam, "read_arming", lambda *a, **k: None)
    never = _Never("execute_operation")
    monkeypatch.setattr(operations, "execute_operation", never)

    with pytest.raises(seam.FirstCandidateRefused):
        seam.dispatch_first_candidate(
            _Conn(),
            project_id="proj_EXAMPLE",
            datastream_id="ds_MISSING",
            actor="owner@example.com",
            today=date(2026, 8, 2),
            idempotency_key="k",
        )
    assert never.calls == 0


# ---------------------------------------------------------------------------
# Publication: one NAMED candidate, carried verbatim to the existing writer.
# ---------------------------------------------------------------------------


def test_publication_refuses_an_identifier_that_is_not_an_execution(monkeypatch) -> None:
    import core.datastream_activation as activation
    import core.datastream_first_candidate as seam

    never_read = _Never("read_candidate_review")
    never_publish = _Never("publish_activate_mutation")
    monkeypatch.setattr(activation, "read_candidate_review", never_read)
    monkeypatch.setattr(activation, "publish_activate_mutation", never_publish)

    for bad in ("latest", "ds_EXAMPLE", "", "dsx_EXAMPLE"):
        with pytest.raises(seam.FirstCandidateRefused):
            seam.publish_activate_candidate(
                _Conn(),
                project_id="proj_EXAMPLE",
                datastream_id="ds_EXAMPLE",
                execution_id=bad,
                actor="owner@example.com",
                idempotency_key="k",
            )
    assert never_read.calls == 0
    assert never_publish.calls == 0


def test_publication_hands_the_exact_named_review_to_the_only_lifecycle_writer(
    monkeypatch,
) -> None:
    import core.datastream_activation as activation
    import core.datastream_first_candidate as seam
    import core.operations as operations

    review = {
        "project_id": "proj_EXAMPLE",
        "datastream_id": "ds_EXAMPLE",
        "execution_id": "dse_NAMED",
        "review_hash": "hash_EXAMPLE",
        "row_count": 42,
        "plan_version_id": "dspv_EXAMPLE",
        "mapping_version_id": "dsmv_EXAMPLE",
        "schedule": {"schedule_mode": "nightly"},
    }
    read = _Recorder(returns=review)
    published = _Recorder(returns="mutation-result")
    monkeypatch.setattr(activation, "read_candidate_review", read)
    monkeypatch.setattr(activation, "publish_activate_mutation", published)

    seen: dict = {}

    def _execute(conn, spec, *, mutation):
        seen["spec"] = spec
        mutation(conn, "op_EXAMPLE")

        class _Result:
            operation_id = "op_EXAMPLE"
            outcome = "succeeded"
            result: dict = {}
            replayed = False

        return _Result()

    monkeypatch.setattr(operations, "execute_operation", _execute)

    returned_review, operation = seam.publish_activate_candidate(
        _Conn(rows=[("org_EXAMPLE",)]),
        project_id="proj_EXAMPLE",
        datastream_id="ds_EXAMPLE",
        execution_id="dse_NAMED",
        actor="owner@example.com",
        idempotency_key="k",
    )

    assert read.calls[0][1]["execution_id"] == "dse_NAMED"
    assert published.calls[0][1]["review"] is review
    assert returned_review is review
    assert operation.operation_id == "op_EXAMPLE"
    spec = seen["spec"]
    assert spec.confirmation_mode == "human"
    assert spec.effective_org_id == "org_EXAMPLE"
    assert spec.request_payload["execution_id"] == "dse_NAMED"


# ---------------------------------------------------------------------------
# Structural guards: the governed path stays the ONLY path.
# ---------------------------------------------------------------------------


def test_neither_new_module_writes_the_datastream_row_directly() -> None:
    for path in (_SEAM, _MCP):
        source = path.read_text(encoding="utf-8")
        assert "UPDATE app.datastreams" not in source, path.name
        assert "lifecycle_state=" not in source, path.name
        assert "lifecycle_state =" not in source, path.name


def test_the_mcp_module_registers_nothing_outside_the_capability_middleware() -> None:
    source = _MCP.read_text(encoding="utf-8")
    assert "register_profiled(" in source
    # A bare `mcp.tool` escapes the profile middleware entirely -- the exact hole
    # `submit_feedback` still has. It must never appear in this module.
    assert "mcp.tool(" not in source
    assert "@mcp.tool" not in source


@pytest.mark.parametrize(
    ("name", "effect", "confirmation_mode"),
    [
        ("get_datastream_arming", "read", "none"),
        ("start_datastream_first_candidate", "confirmed_write", "human"),
        ("publish_activate_datastream_candidate", "confirmed_write", "human"),
    ],
)
def test_each_declaration_is_internally_consistent(name, effect, confirmation_mode) -> None:
    from core.mcp_profiles import ToolDeclaration, _assert_consistent, _reject_unknown

    decl = ToolDeclaration(
        name=name,
        profile="operations",
        effect=effect,
        data_class="operational",
        confirmation_mode=confirmation_mode,
    )
    _reject_unknown("operations", effect, "operational", confirmation_mode)
    _assert_consistent(decl)


def test_no_production_identifier_leaks_into_the_new_files() -> None:
    # This test file is deliberately NOT scanned: it has to spell the forbidden
    # prefixes to look for them, and a self-scan would fail on its own needle.
    forbidden = ("ds_" + "01", "proj_" + "01", "conn_" + "01", "@gmail" + ".com")
    for path in (_SEAM, _MCP):
        source = path.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in source, f"{path.name} carries {needle}"
