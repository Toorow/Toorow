"""Story 57.10 -- what a Datastream's data role COSTS, proved on the ladder itself.

The pure agreement table is already covered, elsewhere and exhaustively
(``test_fee_tax_geo_bridge.py:1070-1128``). This file does not repeat it. It proves the
CONSEQUENCE for one Datastream: ``resolve_source_type`` run end to end -- declaration
read, datastream row read, manifest read off disk -- for the three cases the wizard
creates or removes.

* ``Operational`` -- what migration 093's backfill regex writes when it recognises
  nothing (``093:50``, ``ELSE 'Operational'``), and what the wizard used to be able to
  produce by never asking. It matches no pair, leaves the cost cascade, and the reason
  rendered to an operator is a SENTENCE, not a code;
* a ``NULL`` data role -- the same typed unknown, because a missing signal is a
  disagreement and never a hint;
* ``("paid_media", "Spend")`` -- the pair the wizard now makes reachable, which is the
  only reason any of this matters.

Offline: no Postgres, no network. The connection is a scripted fake and the manifest is a
real file in ``tmp_path``, so the manifest read is exercised rather than mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from core import fee_tax_source_types as fts

# A fictional module. The manifest category is the only thing under test, and using a
# real connector name would tie this file to a catalogue that legitimately changes.
_MODULE = "sample-paid-media"
_DATASTREAM_ID = "dst_57_10"
_PROJECT_ID = "prj_57_10"


class _FakeCursor:
    def __init__(self, script, log: list[tuple[str, Any]]) -> None:
        self._script = script
        self._log = log
        self._rows: list[tuple] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._log.append((sql, params))
        self._rows = list(self._script(sql, params) or ())

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    """Minimal psycopg-shaped connection driven by a ``(sql, params) -> rows`` script."""

    def __init__(self, script) -> None:
        self._script = script
        self.statements: list[tuple[str, Any]] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._script, self.statements)


def _modules_dir(tmp_path: Path, category: str | None) -> Path:
    """A real manifest on disk: ``load_registry_entry`` reads it, nothing is mocked."""
    module_dir = tmp_path / _MODULE
    module_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"name": _MODULE, "display_name": "Sample paid media"}
    if category is not None:
        manifest["public_catalog"] = {"category": category}
    (module_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def _conn(*, data_role: str | None, declaration: tuple[str, str] | None = None) -> _FakeConn:
    def _script(sql: str, _params: Any) -> list[tuple]:
        if "app.datastream_source_types" in sql:
            return [declaration] if declaration else []
        if "app.datastreams" in sql:
            return [(_MODULE, data_role)]
        raise AssertionError(f"unexpected statement: {sql}")

    return _FakeConn(_script)


def _resolve(conn: _FakeConn, modules_dir: Path) -> fts.SourceTypeResolution:
    return fts.resolve_source_type(
        _DATASTREAM_ID, _PROJECT_ID, conn, modules_dir=modules_dir
    )


def test_operational_matches_no_rule_and_says_so_in_a_sentence(tmp_path: Path) -> None:
    """The exact Datastream the wizard used to be able to create by never asking.

    DO NOT "fix" this to PAID_MEDIA. ``Operational`` describes how the pipeline behaves,
    not what a platform billed; a verification feed sits in ``paid_media`` and emits no
    cost metric at all. What the screen owes an operator is the QUESTION, asked at the
    Source step -- not a default that makes this row look classified.
    """
    resolution = _resolve(_conn(data_role="Operational"), _modules_dir(tmp_path, "paid_media"))

    assert resolution.value == fts.UNKNOWN
    assert resolution.source == fts.RESOLVED_BY_DEFAULT
    assert resolution.excluded_from_cost_cascade is True
    # A rendered sentence, not a code: this string reaches an operator as-is.
    reason = resolution.exclusion_reason
    assert reason.count(" ") >= 5
    assert "_" not in reason
    assert "declares its type" in reason


def test_a_missing_data_role_is_the_same_typed_unknown(tmp_path: Path) -> None:
    """A NULL role is a DISAGREEMENT, never a hint to fall back on the category alone."""
    resolution = _resolve(_conn(data_role=None), _modules_dir(tmp_path, "paid_media"))

    assert resolution.value == fts.UNKNOWN
    assert resolution.source == fts.RESOLVED_BY_DEFAULT
    assert resolution.excluded_from_cost_cascade is True
    assert resolution.exclusion_reason.strip()


def test_the_pair_the_wizard_makes_reachable_resolves_to_paid_media(tmp_path: Path) -> None:
    """``Spend`` was not even offered by the role select before 57.10."""
    resolution = _resolve(_conn(data_role="Spend"), _modules_dir(tmp_path, "paid_media"))

    assert resolution.value == fts.PAID_MEDIA
    assert resolution.source == fts.RESOLVED_BY_DERIVATION
    assert resolution.declared_by is None
    assert resolution.excluded_from_cost_cascade is False
    assert resolution.exclusion_reason == ""


def test_a_category_less_manifest_cannot_derive_anything(tmp_path: Path) -> None:
    """The wizard renders this absence rather than guessing, and the ladder agrees."""
    resolution = _resolve(_conn(data_role="Spend"), _modules_dir(tmp_path, None))

    assert resolution.value == fts.UNKNOWN
    assert resolution.excluded_from_cost_cascade is True


def test_the_explicit_declaration_the_screen_names_really_wins(tmp_path: Path) -> None:
    """The wizard tells the operator the category is corrected elsewhere. It is true.

    ``app.datastream_source_types`` is read FIRST, so a declaration overrides a manifest
    category and a data role that would otherwise derive something else. That is why the
    category is shown read-only instead of becoming a second authority on the screen.
    """
    conn = _conn(data_role="Spend", declaration=(fts.LEAD_GEN_MEDIA, "declared-by-operator"))
    resolution = _resolve(conn, _modules_dir(tmp_path, "paid_media"))

    assert resolution.value == fts.LEAD_GEN_MEDIA
    assert resolution.source == fts.RESOLVED_BY_DECLARATION
    assert resolution.declared_by == "declared-by-operator"
    assert resolution.excluded_from_cost_cascade is False


def test_every_one_of_the_seven_data_roles_resolves_without_raising(tmp_path: Path) -> None:
    """What ``:1105-1122`` does not do: bind the sweep to the vocabulary the SCREEN offers.

    That exhaustive sweep runs the PURE table against a literal local to its own file
    (``test_fee_tax_geo_bridge.py:96`` -- a fourth copy of the seven). This one imports
    ``core.datastreams.DATA_ROLES``, the single set the wizard select is now checked
    against, and runs the FULL ladder -- declaration read, row read, manifest off disk --
    for each of them. Add an eighth role and this fails; the sweep would not.
    """
    from core.datastreams import DATA_ROLES

    assert len(DATA_ROLES) == 7
    modules_dir = _modules_dir(tmp_path, "paid_media")
    derived = {
        role: _resolve(_conn(data_role=role), modules_dir).value for role in DATA_ROLES
    }

    assert derived["Spend"] == fts.PAID_MEDIA
    assert derived["Performance"] == fts.PAID_MEDIA
    assert [role for role, value in derived.items() if value == fts.UNKNOWN] == [
        "Revenue & conversions",
        "Forecast & plan",
        "Context",
        "Reference & targets",
        "Operational",
    ]


# An unreadable store degrading to a typed UNKNOWN is NOT retested here: it is
# `test_fee_tax_geo_bridge.py:1315-1322` (`test_36c`), word for word. Nothing in
# story 57.10 changes that path.


if __name__ == "__main__":  # pragma: no cover - convenience only
    raise SystemExit(pytest.main([__file__, "-q"]))
