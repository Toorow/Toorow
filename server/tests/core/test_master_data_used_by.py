"""The used-by guard, and the outage it used to mistake for an empty answer.

``app.market_bindings``'s reader swallowed its exception and returned ``()``.
Its one caller asks *is anything depending on this market?* before a destructive
change -- so a database outage was indistinguishable from a genuine "nothing
depends on it", and the guard let the change through. Story 48.2 (AC8) reverses
that, and these tests are the reversal stated as evidence rather than as a
comment in the code.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from core.master_data import (
    MasterDataConflict,
    MasterDataUnavailable,
    UsedByReference,
    assert_change_acknowledged,
    fetch_used_by,
    registry_summary,
)

REGISTRY = "mdreg_1"
PROJECT = "proj_EXAMPLE"
FRANCE = "mdnode_FR"
RECORDED_AT = datetime(2026, 9, 1, 8, 30, tzinfo=UTC)


class _Cursor:
    def __init__(self, rows, *, fail: bool = False) -> None:
        self._rows, self._fail = rows, fail

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, *_args, **_kwargs) -> None:
        if self._fail:
            raise RuntimeError("connection reset by peer")

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    """The narrowest stand-in that can express an outage: a cursor that raises."""

    def __init__(self, rows=(), *, fail: bool = False) -> None:
        self._rows, self._fail = list(rows), fail

    def cursor(self):
        return _Cursor(self._rows, fail=self._fail)


#: The seven columns `fetch_used_by` selects, in order. `created_at` is the
#: evidence time AC7 requires every reference to carry, and it is the last one.
def _binding_row(consumer_id: str = "bud_EXAMPLE"):
    return (FRANCE, "budget", consumer_id, "FY26 France", "ver_1", "mdver_1", RECORDED_AT)


# ---------------------------------------------------------------------------
# The reversal itself.
# ---------------------------------------------------------------------------


def test_an_unreadable_used_by_store_raises_instead_of_reporting_zero() -> None:
    with pytest.raises(MasterDataUnavailable):
        fetch_used_by(_Conn(fail=True), project_id=PROJECT, registry_id=REGISTRY)


def test_a_readable_empty_store_is_a_real_answer() -> None:
    assert fetch_used_by(_Conn([]), project_id=PROJECT, registry_id=REGISTRY) == ()


def test_live_dependents_are_returned_with_the_version_they_pinned() -> None:
    references = fetch_used_by(_Conn([_binding_row()]), project_id=PROJECT, registry_id=REGISTRY)
    assert references == (
        UsedByReference(
            node_id=FRANCE,
            consumer_kind="budget",
            consumer_id="bud_EXAMPLE",
            consumer_label="FY26 France",
            consumer_version_id="ver_1",
            hierarchy_version_id="mdver_1",
            # `budget` is caller data: no route claims it, so the reference says
            # its owner workspace is unknown rather than inventing one.
            workspace=None,
            recorded_at=RECORDED_AT.isoformat(),
        ),
    )


# ---------------------------------------------------------------------------
# What the guard does with each of those three outcomes.
# ---------------------------------------------------------------------------


def test_a_destructive_change_is_refused_until_the_dependents_are_reviewed() -> None:
    with pytest.raises(MasterDataConflict, match="must be reviewed"):
        assert_change_acknowledged(
            _Conn([_binding_row()]),
            project_id=PROJECT,
            registry_id=REGISTRY,
            affected_node_ids=[FRANCE],
            acknowledged=False,
        )


def test_an_acknowledged_change_proceeds_and_still_reports_what_it_affects() -> None:
    references = assert_change_acknowledged(
        _Conn([_binding_row()]),
        project_id=PROJECT,
        registry_id=REGISTRY,
        affected_node_ids=[FRANCE],
        acknowledged=True,
    )
    assert len(references) == 1
    assert references[0].consumer_id == "bud_EXAMPLE"


def test_an_outage_blocks_the_change_rather_than_authorizing_it() -> None:
    """The whole point: the guard does not catch this and call it zero."""

    with pytest.raises(MasterDataUnavailable):
        assert_change_acknowledged(
            _Conn(fail=True),
            project_id=PROJECT,
            registry_id=REGISTRY,
            affected_node_ids=[FRANCE],
            acknowledged=False,
        )


def test_an_outage_blocks_even_an_acknowledged_change() -> None:
    """Acknowledging a list nobody could read acknowledges nothing."""

    with pytest.raises(MasterDataUnavailable):
        assert_change_acknowledged(
            _Conn(fail=True),
            project_id=PROJECT,
            registry_id=REGISTRY,
            affected_node_ids=[FRANCE],
            acknowledged=True,
        )


def test_no_dependents_needs_no_acknowledgement() -> None:
    assert (
        assert_change_acknowledged(
            _Conn([]),
            project_id=PROJECT,
            registry_id=REGISTRY,
            affected_node_ids=[FRANCE],
            acknowledged=False,
        )
        == ()
    )


# ---------------------------------------------------------------------------
# AC10 -- the MCP summary is bounded, and says what it left out.
# ---------------------------------------------------------------------------


def test_a_project_without_a_registry_summarises_to_none_not_to_empty() -> None:
    assert registry_summary(_Conn([]), project_id=PROJECT, object_kind="country") is None


# ---------------------------------------------------------------------------
# The workspace a reference names must be one the console mounts (49.2 AC7).
# ---------------------------------------------------------------------------


def test_used_by_workspaces_match_the_console() -> None:
    """A composed workspace the console does not mount is an unopenable address.

    `master_data_consumers` derives the owner workspace of every used-by
    reference. Read against the navigation registry rather than against a copy of
    it: the registry is where a workspace is renamed, and a second list free to
    disagree is how a reference comes to name a place that does not exist.
    """
    import re
    from pathlib import Path

    from core.master_data_consumers import (
        _CONSUMER_ROUTES,
        _LINK_TARGET_ROUTES,
        WORKSPACE_LABELS,
    )

    repository = Path(__file__).resolve().parents[3]
    navigation = repository / "ui" / "admin" / "src" / "shell" / "navigation"
    declared = {
        match.group(1)
        for path in navigation.glob("*.ts")
        for match in re.finditer(r'^  key: "([a-z-]+)",$', path.read_text(encoding="utf-8"), re.M)
    }
    assert declared, "the navigation registry was not found where this test looks"
    assert set(WORKSPACE_LABELS) == declared

    routed = {route[0] for route in _CONSUMER_ROUTES.values()}
    routed |= {route[0] for route in _LINK_TARGET_ROUTES.values()}
    assert routed <= declared
