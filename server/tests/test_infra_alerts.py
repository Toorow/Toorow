"""Tests for AI-41: nango_revoke_failed infra alert during project archival.

Also covers write_infra_firing helper in infra_alerts.py (used by both AI-32 and AI-41).

Covers:
  * on Nango revocation failure during archival, write_infra_firing is called
    with alert_type='nango_revoke_failed' and project_id in metadata (AI-41)
  * archival still completes (returns 200) even when revocation raises (best-effort)
  * write_infra_firing does NOT emit nango_revoke_failed when revocation succeeds
  * write_infra_firing in infra_alerts.py writes the correct type to alert_firings
  * write_infra_firing never raises (best-effort)

All tests are mock-based (no live DB, no network calls).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Run an async coroutine synchronously.

    asyncio.run creates a fresh event loop per call: get_event_loop() breaks
    when an earlier async test in the full suite has closed the current loop.
    """
    return asyncio.run(coro)


def _make_delete_project_request(project_id: str = "proj_test"):
    """Build a minimal mock Starlette Request for DELETE /api/projects/{id}."""
    req = MagicMock()
    req.path_params = {"project_id": project_id}
    req.body = AsyncMock(return_value=b"")
    return req


def _run_delete_project(
    project_id: str = "proj_test",
    revoked_rows: list | None = None,
    revoke_side_effect=None,
    mock_write_firing: MagicMock | None = None,
):
    """Drive _delete_project with controlled mocks; return the HTTP response."""
    from core.projects_api import _delete_project  # noqa: PLC0415

    if revoked_rows is None:
        revoked_rows = [("conn_ABC", "meta-ads", "nango_conn_1")]

    # Psycopg mock: cursor returns active project status + revoked rows.
    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = ("active",)
    mock_cur.fetchall.return_value = revoked_rows
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.commit = MagicMock()

    # nango_client mock.
    mock_nango = MagicMock()
    if revoke_side_effect is not None:
        mock_nango.revoke_connection.side_effect = revoke_side_effect
    else:
        mock_nango.revoke_connection.return_value = None

    # Tenant key mock (avoids import errors inside the handler).
    mock_tenant_mod = MagicMock()
    mock_tenant_mod.get_tenant_key_backend.return_value = MagicMock()
    mock_tenant_mod.write_key_audit_row = MagicMock()

    firing_mock = mock_write_firing if mock_write_firing is not None else MagicMock()

    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=(True, "test-user"))),
        # THE ROUTE GUARDS BEFORE IT REVOKES, and it must: `_delete_project`
        # answers `not_found` unless the caller holds `manage` on the Project.
        # With a mocked connection there is no membership to hold, so all four
        # tests measured that refusal instead of the firing they exist for --
        # the route never reached the revocation loop at all.
        #
        # `None` is what the helper returns when access is GRANTED; a caller who
        # may not delete is a different test, and `test_epic43_backend_authorization`
        # is where it lives.
        patch("core.admin_api._refuse_unless_project_allowed", MagicMock(return_value=None)),
        patch("core.db.get_connection", MagicMock(return_value=mock_conn)),
        patch("core.projects_api.nango_client", mock_nango),
        patch("core.projects_api.write_audit_row", MagicMock()),
        patch("core.infra_alerts.write_infra_firing", firing_mock),
        patch.dict("sys.modules", {"core.tenant_keys": mock_tenant_mod}),
    ):
        req = _make_delete_project_request(project_id)
        return _run_async(_delete_project(req)), firing_mock


# ---------------------------------------------------------------------------
# AI-41 -- nango_revoke_failed firing
# ---------------------------------------------------------------------------


def test_nango_revoke_failed_firing_emitted_on_revocation_error():
    """write_infra_firing called with alert_type='nango_revoke_failed' when revoke raises."""
    mock_write_firing = MagicMock()
    response, mock_write_firing = _run_delete_project(
        revoke_side_effect=RuntimeError("Nango 503"),
        mock_write_firing=mock_write_firing,
    )

    # The firing must have been emitted.
    assert mock_write_firing.called, "Expected write_infra_firing to be called"
    found = any(
        (
            call.kwargs.get("alert_type") == "nango_revoke_failed"
            or (call.args and call.args[0] == "nango_revoke_failed")
        )
        for call in mock_write_firing.call_args_list
    )
    assert found, (
        f"Expected write_infra_firing called with alert_type='nango_revoke_failed'. "
        f"Actual calls: {mock_write_firing.call_args_list}"
    )


def test_nango_revoke_failed_firing_includes_project_id():
    """nango_revoke_failed firing metadata includes the project_id."""
    mock_write_firing = MagicMock()
    _run_delete_project(
        project_id="proj_xyz",
        revoke_side_effect=RuntimeError("timeout"),
        mock_write_firing=mock_write_firing,
    )

    # Find the nango_revoke_failed call and check project_id in kwargs.
    nango_fail_calls = [
        c for c in mock_write_firing.call_args_list
        if (
            c.kwargs.get("alert_type") == "nango_revoke_failed"
            or (c.args and c.args[0] == "nango_revoke_failed")
        )
    ]
    assert nango_fail_calls, "Expected nango_revoke_failed firing"
    call = nango_fail_calls[0]
    # project_id must appear either as kwarg or in metadata.
    project_id_kwarg = call.kwargs.get("project_id")
    metadata = call.kwargs.get("metadata") or {}
    assert project_id_kwarg == "proj_xyz" or metadata.get("project_id") == "proj_xyz", (
        f"Expected project_id='proj_xyz' in firing call, got kwargs={call.kwargs}"
    )


def test_archival_succeeds_when_revocation_raises():
    """Archival returns 200 even when Nango revocation raises (best-effort)."""
    response, _ = _run_delete_project(
        revoke_side_effect=ConnectionError("timeout"),
    )
    assert response.status_code == 200, (
        f"Expected 200 from archival despite revoke failure, got {response.status_code}"
    )


def test_no_nango_revoke_firing_when_revocation_succeeds():
    """write_infra_firing for nango_revoke_failed NOT called when revocation succeeds."""
    mock_write_firing = MagicMock()
    response, mock_write_firing = _run_delete_project(
        revoke_side_effect=None,  # success
        mock_write_firing=mock_write_firing,
    )

    assert response.status_code == 200
    nango_fail_calls = [
        c for c in mock_write_firing.call_args_list
        if (
            c.kwargs.get("alert_type") == "nango_revoke_failed"
            or (c.args and c.args[0] == "nango_revoke_failed")
        )
    ]
    assert not nango_fail_calls, (
        f"Expected no nango_revoke_failed firing on success, got {nango_fail_calls}"
    )


# ---------------------------------------------------------------------------
# write_infra_firing unit tests (infra_alerts.py)
# ---------------------------------------------------------------------------


def test_write_infra_firing_inserts_correct_type():
    """write_infra_firing writes the given alert_type to app.alert_firings."""
    from core.infra_alerts import write_infra_firing

    mock_cur = MagicMock()
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.commit = MagicMock()

    with (
        patch("core.db.get_connection", MagicMock(return_value=mock_conn)),
        patch.dict("sys.modules", {"ulid": MagicMock(ULID=MagicMock(return_value="FAKEID"))}),
    ):
        write_infra_firing(
            alert_type="nango_revoke_failed",
            project_id="proj_x",
            metric="nango_revoke",
            severity="error",
            message="test revoke failure",
            metadata={"connection_ref_id": "conn_123"},
        )

    assert mock_cur.execute.called, "Expected cursor.execute to be called"
    call_args = mock_cur.execute.call_args
    sql = call_args.args[0] if call_args.args else ""
    params = call_args.args[1] if len(call_args.args) > 1 else ()
    assert "INSERT INTO app.alert_firings" in sql, f"Expected INSERT statement, got: {sql!r}"
    assert "nango_revoke_failed" in params, (
        f"Expected 'nango_revoke_failed' in INSERT params, got {params}"
    )


def test_write_infra_firing_scheduler_step_degraded():
    """write_infra_firing correctly handles scheduler_step_degraded type."""
    from core.infra_alerts import write_infra_firing

    mock_cur = MagicMock()
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.commit = MagicMock()

    with (
        patch("core.db.get_connection", MagicMock(return_value=mock_conn)),
        patch.dict("sys.modules", {"ulid": MagicMock(ULID=MagicMock(return_value="FAKEID2"))}),
    ):
        write_infra_firing(
            alert_type="scheduler_step_degraded",
            project_id="default",
            metric="scheduler_health",
            severity="error",
            message="steps degraded",
            metadata={"degraded_steps": ["dispatch_nightly"]},
        )

    params = mock_cur.execute.call_args.args[1]
    assert "scheduler_step_degraded" in params, (
        f"Expected 'scheduler_step_degraded' in INSERT params, got {params}"
    )


def test_write_infra_firing_never_raises_on_db_error():
    """write_infra_firing swallows DB exceptions (best-effort, never raises)."""
    from core.infra_alerts import write_infra_firing

    with patch("core.db.get_connection", side_effect=Exception("DB down")):
        # Must not raise.
        write_infra_firing(
            alert_type="scheduler_step_degraded",
            message="some step failed",
        )


def test_write_infra_firing_never_raises_on_missing_ulid():
    """write_infra_firing swallows ImportError for ulid (best-effort)."""
    from core.infra_alerts import write_infra_firing

    with patch.dict("sys.modules", {"ulid": None}):
        # Must not raise.
        write_infra_firing(
            alert_type="nango_revoke_failed",
            message="ulid missing",
        )


# ---------------------------------------------------------------------------
# The day a finding SPEAKS of (story 59.4, arbitrage 5).
#
# The four values above were written as SQL literals -- `observed_value = 0`,
# `threshold = 0`, `pull_ids = '{}'` -- and `window_date` as `date.today()`. The
# tests above this line assert the INSERT and its type; none of them looked at
# what the row SAYS, which is how a finding about the 5th, written on the 8th,
# said the 8th on 2423 rows.
# ---------------------------------------------------------------------------


def test_a_named_window_date_is_the_one_written():
    from datetime import date

    from core.infra_alerts import _firing_window_date

    assert _firing_window_date(date(2026, 8, 5), None) == date(2026, 8, 5)
    assert _firing_window_date("2026-08-05", None) == date(2026, 8, 5)


def test_the_metadata_window_date_repairs_the_checks_that_never_pass_one():
    """THE CLASS REPAIR, and why the fallback exists rather than seven call sites.

    All seven DQ monitors already put the real day in `metadata["window_date"]`,
    where it reached the row only concatenated into `message`. Reading it here
    stops all of them dating a finding "today" without one of them moving.
    """
    from datetime import date

    from core.infra_alerts import _firing_window_date

    assert _firing_window_date(None, {"window_date": "2026-08-05"}) == date(2026, 8, 5)
    # A named date still wins: the caller knows more than the metadata blob.
    assert (
        _firing_window_date(date(2026, 8, 6), {"window_date": "2026-08-05"})
        == date(2026, 8, 6)
    )


def test_an_infra_event_with_no_window_is_still_dated_today():
    """AI-32 and AI-41 are genuinely about now, and a malformed date never raises.

    A firing lost to its own metadata would be an alert deleted by a typo.
    """
    from datetime import date

    from core.infra_alerts import _firing_window_date

    assert _firing_window_date(None, None) == date.today()
    assert _firing_window_date(None, {"degraded_steps": ["mirror_sync"]}) == date.today()
    assert _firing_window_date("not-a-date", {"window_date": ""}) == date.today()


def test_the_insert_carries_the_measurement_and_not_a_literal():
    """The same INSERT the first test in this file asserts, read for its VALUES."""
    from datetime import date

    from core.infra_alerts import write_infra_firing

    mock_cur = MagicMock()
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.commit = MagicMock()

    with (
        patch("core.db.get_connection", MagicMock(return_value=mock_conn)),
        patch.dict("sys.modules", {"ulid": MagicMock(ULID=MagicMock(return_value="FAKEID3"))}),
    ):
        write_infra_firing(
            alert_type="dq_zero_rows",
            project_id="proj_EXAMPLE",
            metric="row_count",
            severity="warning",
            message="the window returned nothing",
            metadata={"window_date": "2026-08-05"},
            observed_value=0,
            threshold=1,
            pull_ids=["pull_EXAMPLE"],
            window_date=date(2026, 8, 6),
        )

    sql = mock_cur.execute.call_args.args[0]
    params = mock_cur.execute.call_args.args[1]
    assert "'{}'" not in sql, "pull_ids was a literal in the statement"
    assert 0.0 in params and 1.0 in params
    assert ["pull_EXAMPLE"] in params
    assert date(2026, 8, 6) in params
    assert date.today() not in params, "the finding is dated by its window, not by today"


# ---------------------------------------------------------------------------
# Firing messages are ENGLISH (story 59.4).
#
# A person reads `alert_firings.message`. Seven checks wrote it and five of them
# wrote French; the seventh, `dq_schema`, was missed by TWO rounds of review
# because it is French with no accented character -- "Schema modifie pour ... (+2
# colonnes)" survives any sweep that greps for accents. So this guard reads the
# call sites themselves and looks at WORDS, and it is why it exists: the rule
# lived only in CLAUDE.md, so every check added was free to break it again.
# ---------------------------------------------------------------------------

#: The marker list lives in `tests/english_guard.py` since story 59.5, and the
#: RUNTIME half of this guard reads the same one. Two copies of a word list is how
#: the static half and the runtime half start disagreeing about what French is.
from tests.english_guard import FRENCH_MARKERS as _FRENCH_MARKERS  # noqa: E402

_WORD = __import__("re").compile(r"[A-Za-z]+")


def _firing_messages() -> tuple[list[tuple[str, str, str]], list[str]]:
    """``(messages, unreadable)`` for every firing message in `server/core`.

    ``messages`` is ``(module, label, text)``; ``unreadable`` names each firing
    whose message this scanner could NOT resolve to text, with the expression it
    gave up on. That second list is the point: a message it cannot read is a
    message it does not check, and a guard that records those as covered reports
    a clean sweep over the sentence it never saw.

    A PARTIAL READ IS NOT A READ, and that was this guard's last hole. Every real
    message is an f-string, so joining the literal parts and skipping the
    interpolated ones scored ``f"Schema {_schema_tail(ds, added)}"`` as the fully
    read string ``"Schema "`` -- French shipping under three green tests. Each
    interpolated expression is therefore RESOLVED:

    * a call to a function of the same module, a module-level constant, or a
      subscript into a module-level dict can carry PROSE, so it is followed to its
      literals; when it cannot be followed, the whole message is unreadable;
    * a local, a parameter, an attribute or a value builtin (`len`, `int`, `str`)
      carries a runtime VALUE -- a Datastream name, a row count -- which no
      vocabulary check can or should judge. It contributes nothing and is not a
      failure. Flagging every interpolation instead would fail all seven real
      messages and teach nobody anything.
    """
    import ast
    import importlib
    import pathlib

    _VALUE_BUILTINS = {
        "len", "int", "float", "str", "round", "abs", "sorted", "sum", "min", "max",
        "repr", "list", "tuple", "set", "dict", "bool",
    }

    class _Reader:
        """Resolves an expression to the prose it can carry, within ONE module."""

        def __init__(self, tree):
            self.tree_root = tree
            self.bindings: dict = {}
            self.functions: dict = {}
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            self.bindings[target.id] = node.value
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.functions[node.name] = node

        def read(self, node, depth: int = 0) -> tuple[list[str], bool]:
            """``(texts, followed)``; ``followed`` is False when prose may hide."""
            # The longest real chain is five hops -- str() -> subscript -> local
            # assignment -> function -> its f-string parts -- so the ceiling is
            # generous enough for prose and still a stop against recursion.
            if node is None or depth > 12:
                return [], False
            if isinstance(node, ast.Constant):
                return ([node.value], True) if isinstance(node.value, str) else ([], True)
            if isinstance(node, ast.JoinedStr):
                return self._merge(self.read(part, depth + 1) for part in node.values)
            if isinstance(node, ast.FormattedValue):
                return self.read(node.value, depth + 1)
            if isinstance(node, ast.BinOp):
                return self._merge(
                    [self.read(node.left, depth + 1), self.read(node.right, depth + 1)]
                )
            if isinstance(node, ast.IfExp):
                return self._merge(
                    [self.read(node.body, depth + 1), self.read(node.orelse, depth + 1)]
                )
            if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                # `" ".join(_PARTS)` where `_PARTS` is a module-level list of
                # words: the prose is in the ELEMENTS, and reading only the
                # separator scored that message as fully read.
                return self._merge(self.read(item, depth + 1) for item in node.elts)
            if isinstance(node, ast.Dict):
                return self._merge(self.read(value, depth + 1) for value in node.values)
            if isinstance(node, ast.Name):
                # A module-level binding may hold prose; a local holds a value.
                if node.id in self.bindings:
                    return self.read(self.bindings[node.id], depth + 1)
                if node.id in self.functions:
                    return self.function_texts(node.id, depth + 1)
                return [], True
            if isinstance(node, ast.Attribute):
                return [], True
            if isinstance(node, ast.Subscript):
                base = node.value
                if isinstance(base, ast.Name):
                    if base.id in self.bindings:
                        return self._subscript(self.bindings[base.id], node.slice, depth)
                    # A LOCAL holding a composed payload -- `payload["message"]`,
                    # where `payload = build_dq_firing_payload(...)`. Following the
                    # assignment is what reads geography's sentence; refusing to
                    # would call a message unreadable that plainly is not.
                    local = self.local_binding(base.id)
                    if local is not None:
                        return self.read(local, depth + 1)
                return [], True
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    # `str(x)` of prose is prose: unwrap rather than score it a value.
                    if func.id == "str" and len(node.args) == 1:
                        return self.read(node.args[0], depth + 1)
                    if func.id in _VALUE_BUILTINS:
                        return [], True
                    if func.id in self.functions:
                        return self.function_texts(func.id, depth + 1)
                    return [], False
                if isinstance(func, ast.Attribute):
                    # `", ".join(parts)`, `template.format(...)`: the receiver and
                    # the arguments are where prose would be.
                    parts = [self.read(func.value, depth + 1)]
                    parts += [self.read(arg, depth + 1) for arg in node.args]
                    return self._merge(parts)
                return [], False
            return [], True

        def local_binding(self, name: str):
            """The value a local of this module is assigned, or `None`.

            Module-level bindings are looked up first (they can hold prose
            directly); this covers the composed-payload shape, where the prose is
            one hop away inside a function.
            """
            for node in ast.walk(self.tree_root):
                if not isinstance(node, ast.Assign):
                    continue
                if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                    return node.value
            return None

        def _subscript(self, container, key, depth: int) -> tuple[list[str], bool]:
            if isinstance(container, ast.Dict) and isinstance(key, ast.Constant):
                for entry_key, entry_value in zip(container.keys, container.values):
                    if isinstance(entry_key, ast.Constant) and entry_key.value == key.value:
                        return self.read(entry_value, depth + 1)
                return [], False
            return [], False

        def function_texts(self, name: str, depth: int = 0) -> tuple[list[str], bool]:
            """What a same-module function can put into a message."""
            node = self.functions.get(name)
            if node is None:
                return [], False
            parts: list = []
            for inner in ast.walk(node):
                if isinstance(inner, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "message" for t in inner.targets
                ):
                    parts.append(self.read(inner.value, depth + 1))
                elif isinstance(inner, ast.Return) and inner.value is not None:
                    parts.append(self.read(inner.value, depth + 1))
                    if isinstance(inner.value, ast.Dict):
                        for key, value in zip(inner.value.keys, inner.value.values):
                            if isinstance(key, ast.Constant) and key.value == "message":
                                parts.append(self.read(value, depth + 1))
            if not parts:
                return [], False
            return self._merge(parts)

        @staticmethod
        def _merge(results) -> tuple[list[str], bool]:
            texts: list[str] = []
            followed = True
            for part_texts, part_followed in results:
                texts.extend(part_texts)
                followed = followed and part_followed
            return texts, followed

    def _resolve_label(node, module_name: str, reader) -> str:
        texts, _followed = reader.read(node)
        if texts:
            return texts[0]
        try:
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                return str(getattr(importlib.import_module("core." + node.value.id), node.attr))
            if isinstance(node, ast.Name):
                return str(getattr(importlib.import_module("core." + module_name), node.id))
        except (ImportError, AttributeError):
            return ""
        return ""

    # STORY 59.5, THE FIRST OF THE TWO HOLES AI-232 LEFT. A module that fires
    # through a WRAPPER never spelled `write_infra_firing`, so it was skipped
    # whole -- and the message it composed at the wrapper's call site was never
    # read. The wrappers are discovered rather than listed: a function of
    # `server/core` whose body calls `write_infra_firing` IS a firing writer, and
    # a call to it that carries its own `message=` is a firing message.
    def _wrapper_names(paths) -> set[str]:
        names: set[str] = set()
        for path in paths:
            source = path.read_text(encoding="utf-8")
            if "write_infra_firing" not in source:
                continue
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name == "write_infra_firing":
                    continue
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and (
                        getattr(inner.func, "attr", getattr(inner.func, "id", ""))
                        == "write_infra_firing"
                    ):
                        names.add(node.name)
                        break
        return names

    found: list[tuple[str, str, str]] = []
    unreadable: list[str] = []
    core = pathlib.Path(__file__).resolve().parents[1] / "core"
    wrappers = _wrapper_names(sorted(core.glob("*.py")))
    for path in sorted(core.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        # Modules that FIRE. A `message` composed in a module that never calls
        # `write_infra_firing` belongs to another surface with its own rules --
        # `currency_refusal.py` composes refusal sentences that reach a report
        # envelope, not `app.alert_firings`.
        #
        # THE CLASS THIS GUARD LEAVES, named so nobody rediscovers it as news:
        # `currency_refusal.py:257-261` ships a half-French sentence, pre-existing
        # at HEAD and owned by that surface. The OTHER half of the class -- a
        # literal shell with prose smuggled through an interpolation, at any of
        # the seven call sites -- is closed by the resolution above.
        # Story 59.5: a module that IMPORTS `infra_alerts` is scanned even when it
        # never spells `write_infra_firing` -- it may fire through one of the
        # wrappers above, carrying its own message.
        if "write_infra_firing" not in source and "infra_alerts" not in source:
            continue
        tree = ast.parse(source)
        reader = _Reader(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            if name != "write_infra_firing":
                # A wrapper call is a firing site ONLY when the message travels
                # with it. When the wrapper composes its own sentence, that
                # sentence is read where it is written, and reading the call
                # again would report it unreadable for having no `message`.
                if name not in wrappers or "message" not in keywords:
                    continue
            label = (
                _resolve_label(keywords["alert_type"], path.stem, reader)
                if "alert_type" in keywords
                else ""
            )
            expression = keywords.get("message")
            texts, followed = reader.read(expression) if expression is not None else ([], False)
            if followed and texts:
                found.append((path.name, label, " ".join(texts)))
                continue
            dumped = ast.dump(expression)[:90] if expression is not None else "absent"
            unreadable.append(
                f"{path.name}:{node.lineno} alert_type={label or '?'} expression={dumped}"
            )
    return found, unreadable


def test_no_firing_message_is_unreadable():
    """A message the scanner cannot resolve is NOT covered, and that is a failure.

    THE HOLE THIS CLOSES, and it was found by mutation and not by reading: move
    `dq_schema`'s French sentence into a `_compose_schema_message()` helper and the
    two tests below both stayed green while French shipped. The scanner recorded
    the alert type with an empty message "so the type still counts as covered".

    A renamed call site was already caught. An unreadable message is now caught the
    same way, so `governance.md`'s claim that this fails on the eighth check as
    readily as on the first is a claim with a test under it.

    SIX ATTACKS, SIX REDS -- run against `dq_schema` and kept here because a guard
    is worth what it refuses, not what it asserts:

    ===================================================  ================
    mutation                                             result
    ===================================================  ================
    ``f"Schema {_schema_tail(ds, added)}"``               1 failed
    helper returning the French sentence                  1 failed
    module constant + ``.format()``                       1 failed
    ``" ".join(_PARTS)`` over a module-level word list     1 failed
    message behind an imported (unfollowable) call         2 failed
    f-string -> module dict -> ``join`` over a list        3 failed
    ===================================================  ================

    The fourth and sixth were found by running the table, not by reading the code:
    the scanner read the separator and scored the message as fully read while the
    words lived in the list. An attack list that is never executed is a comment.
    """
    _messages, unreadable = _firing_messages()
    assert not unreadable, (
        "firing message(s) this guard cannot read, and therefore cannot check: "
        + "; ".join(unreadable)
        + " -- compose the message at the call site, or extend the scanner."
    )


def test_the_scanner_finds_every_dq_check_that_fires():
    """A guard whose finder breaks is a guard that passes. This is its self-check.

    STORY 59.5, THE SECOND HOLE AI-232 LEFT: this list held SEVEN types and was
    written by hand, so it did not expect the `dq_geography` the scanner reads --
    a check could be dropped from the scan and this self-check would still be
    green. It is now `dq_monitor_registry.FIRING_ALERT_TYPES`, so a monitor added
    to the registry is a monitor this guard demands a readable message for.
    """
    from core.dq_monitor_registry import FIRING_ALERT_TYPES

    messages, _unreadable = _firing_messages()
    types = {alert_type for _module, alert_type, _message in messages}
    for expected in FIRING_ALERT_TYPES:
        assert expected in types, f"{expected} has no message the scanner can read"


def test_every_firing_message_is_english():
    """`alert_firings.message` is read by a person, and the repository is English."""
    messages, _unreadable = _firing_messages()
    offenders: list[str] = []
    for module, alert_type, message in messages:
        words = {word.lower() for word in _WORD.findall(message)}
        french = sorted(words & _FRENCH_MARKERS)
        if french:
            offenders.append(f"{module} {alert_type or '?'}: {french} in {message!r}")
    assert not offenders, "non-English firing message(s):\n" + "\n".join(offenders)
