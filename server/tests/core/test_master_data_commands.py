"""Story 49.2 -- the guarded Master Data commands are auditable and reachable.

`governance.md` asks for two things in one sentence: an *auditable* lifecycle,
and a guard on acts that change what existing rows mean. The guard landed first
(`test_master_data_guarded_commands.py`). These tests hold the other half:

* the command runs through `execute_operation`, so mutation, audit and outbox
  commit together and a replayed idempotency key acts once;
* a refusal happens BEFORE the operation, so a refused command leaves no
  operation row -- a refusal is not an act that happened;
* the impact is recorded on the path that proceeds, so an acknowledged archive
  says what it went ahead despite.

WHAT THE CUTOVER OF 2026-08-25 ADDED TO THIS FILE. `create` and `rename` exist
now, and every node command resolves its SCOPE first: a converged Business
Domain is an organization identity (`project_id IS NULL`), so the Project-scoped
writers this module used to call unconditionally updated no row for it. The
first read of every command is therefore `fetch_org_node`, and these fixtures
answer it -- `[]` for the Project-scoped identities that were the only case
before.

The full organization walk (registry, published version, membership, projection)
is measured against a real database in
`tests/integration/test_master_data_identity_commands_pg.py`: none of it is
provable against a cursor that answers whatever the fixture tells it to.
"""

from __future__ import annotations

from typing import Any

import pytest
from core.master_data import MasterDataUnavailable
from core.master_data_commands import (
    CONVERGE_FIRST_CODE,
    DUPLICATE_SLUG_CODE,
    MASTER_DATA_NODE_COMMAND,
    STALE_VERSION_CODE,
    SUPPORTED_ACTIONS,
    MasterDataCommandRefused,
    create_business_identity,
    run_node_command,
)

PROJECT, ORG, NODE = "proj_EXAMPLE", "org_EXAMPLE", "mdn_1"

_NODE_ROW = (
    NODE, ORG, PROJECT, "reg_1", "market", "France",
    None, None, "person_1", None, None,
)

#: The first read of every node command is the organization lookup. A Project
#: identity is absent from it, which is what routes the command to the
#: Project-scoped writers.
_NOT_AN_ORG_NODE: list = []

#: What `master_data.node_versions_per_identity` answers, and it is read on a
#: RENAME only (AI-328). `node` is a Product, an Activity or a business identity
#: -- each keeps a history of its own, so a rename mints a revision on it.
#: `registry` is Country's shape: the grouping publishes as one act.
_VERSIONS_PER_NODE: list = [("node",)]
_VERSIONS_PER_REGISTRY: list = [("registry",)]


class _Cursor:
    def __init__(self, results: list, *, explode: bool = False):
        self._results = list(results)
        self._current: list = []
        self._explode = explode

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        if self._explode:
            raise RuntimeError("used-by store offline")
        self._current = self._results.pop(0) if self._results else []

    def fetchall(self) -> list:
        return self._current

    def fetchone(self):
        return self._current[0] if self._current else None


class _Conn:
    def __init__(self, results: list, *, explode: bool = False):
        self.cur = _Cursor(results, explode=explode)

    def cursor(self) -> _Cursor:
        return self.cur


class _BreaksAfter:
    """Answers the reads that come BEFORE the one under test, then breaks.

    `explode=True` on `_Conn` fails every read, including the lookups that route
    the command -- which would prove the fail-closed behaviour of the wrong
    query. Here the leading reads answer honestly and the next one is the outage.
    """

    def __init__(self, *answers: list):
        self._answers = list(answers)
        self._current: list = []
        self.cur = self

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        if not self._answers:
            raise RuntimeError("the store is offline")
        self._current = self._answers.pop(0)

    def fetchall(self) -> list:
        return self._current

    def fetchone(self):
        return self._current[0] if self._current else None


@pytest.fixture
def captured(monkeypatch) -> dict[str, Any]:
    """Replace execute_operation so the SPEC it receives can be asserted."""
    seen: dict[str, Any] = {}

    class _Result:
        operation_id = "op_1"
        outcome = "succeeded"
        replayed = False

        def __init__(self, result):
            self.result = result

    def fake_execute(conn, spec, *, mutation):
        seen["spec"] = spec
        outcome = mutation(conn, "op_1")
        seen["mutation"] = outcome
        return _Result(outcome.result)

    monkeypatch.setattr("core.master_data_commands.execute_operation", fake_execute)
    return seen


def test_a_refused_archive_never_reaches_the_operation(captured) -> None:
    """A refusal is not an act: it must not mint an operation row."""
    conn = _Conn(
        [_NOT_AN_ORG_NODE, [_NODE_ROW], [("datastream_mapping", "ds_1", "Meta Ads", None)]]
    )

    with pytest.raises(MasterDataCommandRefused) as exc:
        run_node_command(
            conn, project_id=PROJECT, org_id=ORG, node_id=NODE,
            action="archive", actor="person_1", idempotency_key="k1",
        )

    assert "spec" not in captured
    payload = exc.value.as_dict()
    assert payload["code"] == "master_data_command_refused"
    # The named consumers travel with the refusal so an operator can decide.
    assert payload["impact"]["summary"] == "datastream_mapping (1)"
    assert payload["impact"]["consumers"][0]["consumer_id"] == "ds_1"


def test_an_unreadable_used_by_store_stops_the_command_before_the_operation(captured) -> None:
    conn = _BreaksAfter(_NOT_AN_ORG_NODE, [_NODE_ROW])

    with pytest.raises(MasterDataUnavailable):
        run_node_command(
            conn, project_id=PROJECT, org_id=ORG, node_id=NODE,
            action="archive", actor="person_1", idempotency_key="k1",
        )

    assert "spec" not in captured


def test_a_clear_archive_runs_as_a_durable_operation(captured) -> None:
    conn = _Conn([_NOT_AN_ORG_NODE, [_NODE_ROW], [], [_NODE_ROW]])

    out = run_node_command(
        conn, project_id=PROJECT, org_id=ORG, node_id=NODE,
        action="archive", actor="person_1", idempotency_key="k1",
    )

    spec = captured["spec"]
    assert spec.command_type == MASTER_DATA_NODE_COMMAND
    assert spec.effective_org_id == ORG
    assert spec.resource_path == (
        f"organization:{ORG}", f"project:{PROJECT}", "governance:master-data", f"node:{NODE}",
    )
    assert spec.idempotency_key == "k1"
    assert out["operation_id"] == "op_1"
    assert out["result"]["action"] == "archive"
    assert out["result"]["consumers_at_command_time"] == []
    assert out["result"]["impact_acknowledged"] is False


def test_an_acknowledged_archive_records_what_it_went_ahead_despite(captured) -> None:
    """Otherwise a human decision becomes an unexplained state change."""
    conn = _Conn(
        [_NOT_AN_ORG_NODE, [_NODE_ROW], [("analyze_filter", "flt_1", "Markets", None)], [_NODE_ROW]]
    )

    out = run_node_command(
        conn, project_id=PROJECT, org_id=ORG, node_id=NODE,
        action="archive", actor="person_1", idempotency_key="k1",
        acknowledge_impact=True,
    )

    assert out["result"]["impact_acknowledged"] is True
    consumers = out["result"]["consumers_at_command_time"]
    assert [c["consumer_id"] for c in consumers] == ["flt_1"]


def test_restore_is_a_command_of_its_own_and_needs_no_acknowledgement(captured) -> None:
    conn = _Conn([_NOT_AN_ORG_NODE, [_NODE_ROW], [], [_NODE_ROW]])

    out = run_node_command(
        conn, project_id=PROJECT, org_id=ORG, node_id=NODE,
        action="restore", actor="person_1", idempotency_key="k2",
    )

    assert out["result"]["action"] == "restore"
    # Restore adds an identity back; it removes nothing, so no guard applies.
    assert captured["mutation"].outcome == "succeeded"


def test_a_rename_of_a_registry_scoped_identity_changes_the_label_and_nothing_else(
    captured,
) -> None:
    """The label is a projection; the id, and every binding on it, survive.

    This identity's registry versions as one GROUPING (Country's shape), which
    the scope read below answers `registry` for. A node version minted here would
    open a second history beside the registry's own, so the rename moves the
    label and nothing else -- and `None` is then the honest base, stated rather
    than omitted, because the node genuinely holds no revision.
    """
    renamed = (
        NODE, ORG, PROJECT, "reg_1", "market", "Metropolitan France",
        None, None, "person_1", None, None,
    )
    conn = _Conn(
        [_NOT_AN_ORG_NODE, [_NODE_ROW], _VERSIONS_PER_REGISTRY, [], [], [], [renamed]]
    )

    out = run_node_command(
        conn, project_id=PROJECT, org_id=ORG, node_id=NODE,
        action="rename", actor="person_1", idempotency_key="k3",
        label="Metropolitan France", expected_version=None,
    )

    assert out["result"]["action"] == "rename"
    assert out["result"]["label"] == "Metropolitan France"
    assert captured["mutation"].before_hash != captured["mutation"].after_hash
    assert out["result"]["version_id"] is None


def test_a_rename_without_a_name_is_refused_before_anything_is_read(captured) -> None:
    conn = _Conn([_NOT_AN_ORG_NODE, [_NODE_ROW]])

    with pytest.raises(ValueError, match="label is required"):
        run_node_command(
            conn, project_id=PROJECT, org_id=ORG, node_id=NODE,
            action="rename", actor="person_1", idempotency_key="k4", label="   ",
            expected_version=None,
        )

    assert "spec" not in captured


# ---------------------------------------------------------------------------
# The rename states the base it renames from (governance.md, 2026-08-30).
# ---------------------------------------------------------------------------


def test_a_rename_that_states_no_base_never_reaches_the_authority(captured) -> None:
    """Omitting the precondition is refused, not read as "I found no revision".

    This is the whole difference between a guard and a decoration. If an absent
    `expected_version` defaulted to `None`, every door that simply forgot to send
    one would be treated as a door that looked and saw nothing -- and since a
    Project identity legitimately has no published revision, that default would
    PASS. The command would then carry a precondition that can never fire, which
    is the failure `governance.md` recorded as open work rather than ship.
    """
    conn = _Conn([_NOT_AN_ORG_NODE, [_NODE_ROW]])

    with pytest.raises(ValueError, match="expected_version is required"):
        run_node_command(
            conn, project_id=PROJECT, org_id=ORG, node_id=NODE,
            action="rename", actor="person_1", idempotency_key="k5",
            label="Metropolitan France",
        )

    assert "spec" not in captured


def test_a_rename_on_a_base_the_identity_has_left_is_refused_by_name(captured) -> None:
    """And it is refused BEFORE the operation, so it mints no row at all."""
    conn = _Conn([_NOT_AN_ORG_NODE, [_NODE_ROW], _VERSIONS_PER_NODE, [], [("mdver_9",)]])

    with pytest.raises(MasterDataCommandRefused) as refused:
        run_node_command(
            conn, project_id=PROJECT, org_id=ORG, node_id=NODE,
            action="rename", actor="person_1", idempotency_key="k6",
            label="Metropolitan France", expected_version="mdver_1",
        )

    assert refused.value.code == STALE_VERSION_CODE
    # The sentence names the state and the gesture. "could not be saved" would
    # send the person back to press Save on the same stale edit.
    assert "Reload it" in str(refused.value)
    assert "spec" not in captured


def test_the_base_is_read_again_under_the_lock_and_a_race_loses_there(captured) -> None:
    """The unlocked reading cannot decide a race; only the locked one can.

    Both concurrent renames read the SAME base before the operation, so both pass
    that check. The second reading, inside the mutation and under `FOR UPDATE`,
    is where the loser sees the revision the winner published -- and it refuses
    under the same name, having written nothing: the operation row inserted a
    moment earlier rolls back with the caller's transaction.
    """
    conn = _Conn(
        [
            _NOT_AN_ORG_NODE, [_NODE_ROW], _VERSIONS_PER_NODE, [],
            [("mdver_1",)], [("mdver_2",)],
        ]
    )

    with pytest.raises(MasterDataCommandRefused) as refused:
        run_node_command(
            conn, project_id=PROJECT, org_id=ORG, node_id=NODE,
            action="rename", actor="person_1", idempotency_key="k7",
            label="Metropolitan France", expected_version="mdver_1",
        )

    assert refused.value.code == STALE_VERSION_CODE
    # It got past the pre-check -- the spec exists -- and no mutation completed.
    assert captured["spec"].command_type == MASTER_DATA_NODE_COMMAND
    assert "mutation" not in captured


def test_the_before_and_after_hashes_differ_so_the_audit_records_a_change(captured) -> None:
    conn = _Conn([_NOT_AN_ORG_NODE, [_NODE_ROW], [], [_NODE_ROW]])

    run_node_command(
        conn, project_id=PROJECT, org_id=ORG, node_id=NODE,
        action="archive", actor="person_1", idempotency_key="k1",
    )

    mutation = captured["mutation"]
    assert mutation.before_hash != mutation.after_hash
    assert mutation.outbox_payload == {
        "project_id": PROJECT, "node_id": NODE, "action": "archive",
    }


@pytest.mark.parametrize("action", ["", "delete", "merge", "regroup", "create"])
def test_an_unsupported_node_action_is_refused_by_name(action: str, captured) -> None:
    """merge and regroup are absent on purpose: they need a diff and a preview.

    `create` is refused HERE and supported elsewhere: there is no node id to
    address before the identity exists, so it is its own function and its own
    route rather than an action carrying an id nobody minted yet.
    """
    conn = _Conn([_NOT_AN_ORG_NODE, [_NODE_ROW]])

    with pytest.raises(ValueError, match="action must be one of"):
        run_node_command(
            conn, project_id=PROJECT, org_id=ORG, node_id=NODE,
            action=action, actor="person_1", idempotency_key="k1",
        )

    assert "spec" not in captured
    assert "merge" not in SUPPORTED_ACTIONS
    assert "create" in SUPPORTED_ACTIONS


# ---------------------------------------------------------------------------
# Creation. Every refusal below happens BEFORE the operation, and each one names
# a different gesture -- which is why they carry different codes.
# ---------------------------------------------------------------------------


def _pending_plan(monkeypatch, *, domains: int, classifications: int = 0) -> None:
    from core.master_data_convergence import ConvergencePlan

    plan = ConvergencePlan(
        org_id=ORG,
        domains=tuple({"id": f"bdm_{i}"} for i in range(domains)),
        classifications=tuple({"id": f"bcl_{i}"} for i in range(classifications)),
    )
    monkeypatch.setattr(
        "core.master_data_commands.plan_convergence", lambda conn, *, org_id: plan
    )


def _converged(monkeypatch) -> None:
    _pending_plan(monkeypatch, domains=0)


def test_an_unconverged_organization_is_refused_and_told_which_gesture_comes_first(
    captured, monkeypatch
) -> None:
    """No window in which two authorities write for the same organization.

    The console does not draw the create control on an unconverged organization;
    this is the same rule at the THRESHOLD, so a script or an MCP tool meets it
    too. The sentence names the convergence and no store, column or deployment
    state.
    """
    _pending_plan(monkeypatch, domains=6, classifications=2)
    conn = _Conn([])

    with pytest.raises(MasterDataCommandRefused) as exc:
        create_business_identity(
            conn, project_id=PROJECT, org_id=ORG, kind="business_domain",
            name="Retail Media", actor="person_1", idempotency_key="k1",
        )

    payload = exc.value.as_dict()
    assert payload["code"] == CONVERGE_FIRST_CODE
    assert "converge" in payload["message"].lower()
    for forbidden in ("mdm_business_domains", "master_data_nodes", "column", "deploy"):
        assert forbidden not in payload["message"]
    assert payload["impact"]["pending"]["pending_domains"] == 6
    assert "spec" not in captured


def test_a_taken_short_code_is_refused_under_its_own_code(captured, monkeypatch) -> None:
    """Two 409s, two repairs. Collapsing them sends a person to the wrong field."""
    _converged(monkeypatch)
    conn = _Conn([[], [("bdm_existing",)]])  # no earlier operation, then the taken code

    with pytest.raises(MasterDataCommandRefused) as exc:
        create_business_identity(
            conn, project_id=PROJECT, org_id=ORG, kind="business_domain",
            name="Retail Media", actor="person_1", idempotency_key="k1",
        )

    assert exc.value.as_dict()["code"] == DUPLICATE_SLUG_CODE
    assert "retail-media" in str(exc.value)
    assert "spec" not in captured


def test_an_unreadable_taxonomy_fails_closed_rather_than_minting_a_twin(
    captured, monkeypatch
) -> None:
    _converged(monkeypatch)
    conn = _BreaksAfter([])  # the idempotency lookup answers; the taxonomy does not

    with pytest.raises(MasterDataUnavailable):
        create_business_identity(
            conn, project_id=PROJECT, org_id=ORG, kind="business_domain",
            name="Retail Media", actor="person_1", idempotency_key="k1",
        )

    assert "spec" not in captured


@pytest.mark.parametrize(
    ("kind", "name", "expected"),
    [
        ("", "Retail Media", "kind must be one of"),
        ("market", "Retail Media", "kind must be one of"),
        ("business_domain", "   ", "name is required"),
    ],
)
def test_a_creation_outside_the_two_business_kinds_is_refused_by_name(
    kind: str, name: str, expected: str, captured, monkeypatch
) -> None:
    """A market and a competitor are minted by the capability that owns them."""
    _converged(monkeypatch)
    conn = _Conn([])

    with pytest.raises(ValueError, match=expected):
        create_business_identity(
            conn, project_id=PROJECT, org_id=ORG, kind=kind, name=name,
            actor="person_1", idempotency_key="k1",
        )

    assert "spec" not in captured


def test_a_classification_without_a_parent_domain_is_refused(captured, monkeypatch) -> None:
    """The server refuses it rather than filing an orphan under nothing."""
    _converged(monkeypatch)
    conn = _Conn([])

    with pytest.raises(ValueError, match="domain_node_id is required"):
        create_business_identity(
            conn, project_id=PROJECT, org_id=ORG, kind="business_classification",
            name="Paid Social", classification_type="product_line",
            actor="person_1", idempotency_key="k1",
        )

    assert "spec" not in captured


def test_a_classification_without_its_type_is_refused(captured, monkeypatch) -> None:
    """`classification_type` is the word the organization chose; it is required
    by the published type version, so the command asks for it rather than
    minting a version the schema would reject."""
    _converged(monkeypatch)
    conn = _Conn([])

    with pytest.raises(ValueError, match="classification_type is required"):
        create_business_identity(
            conn, project_id=PROJECT, org_id=ORG, kind="business_classification",
            name="Paid Social", domain_node_id="bd_1",
            actor="person_1", idempotency_key="k1",
        )

    assert "spec" not in captured


def test_the_guard_reads_the_used_by_store_once_and_never_the_retired_bridge() -> None:
    """MUTATION: put `domain_used_by` back in the impact -> this goes red.

    Two readers of the same fact are two answers to one question. Until
    2026-08-25 `business_taxonomy.domain_used_by` was the ONLY reader that could
    say whether a Semantic Model version still named a Business Domain, because
    `app.master_data_used_by` could not physically hold a row about an
    organization node (migration 140's composite foreign key). Migration 311 and
    `semantic_model_used_by.register_published_version` landed the same day, so
    the store answers it now -- and reading both would count every consumer twice
    and refuse an archive naming two of everything.

    The used-by half is read through `master_data.assess_org_node_impact`, the
    store's own organization reader, rather than a second query written here.
    """
    import inspect

    from core import master_data_commands

    # The module docstring names the retired bridge on purpose -- that sentence
    # is what stops it being re-added by someone who cannot see why it went. What
    # must not exist is a CALL.
    body = inspect.getsource(master_data_commands).split('"""', 2)[-1]
    assert "domain_used_by" not in body
    assert "assess_org_node_impact" in body
    # The links are the half this module DOES own: the convergence does not move
    # them, so the used-by store never holds them.
    assert "mdm_business_links" in body
