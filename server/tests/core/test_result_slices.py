"""Story 50.6 (AC5, AC6, AC7, AC13) -- the handle is not a credential, proved in code.

TWO KINDS OF TEST LIVE HERE, AND THE SECOND KIND IS THE POINT.

Behavioural tests show that today's reader refuses what it must refuse. They are
necessary and they are not sufficient: a future edit can add a warehouse import,
an INSERT or a second `_meta` builder and every behavioural test still passes.

So the four assertions AC6 property 4 and AC5 rest on are written as SOURCE
INSPECTION. They fail when someone reintroduces the escape hatch, which is the
only moment they matter. `visualization-and-rendering.md` says the handle is
"neither a credential nor a warehouse query escape hatch"; these tests are the
mechanism behind that sentence rather than a restatement of it.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from core import result_slices
from core.result_slices import (
    MAX_CURSOR_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_SLICE_COLUMNS,
    MAX_SLICE_OFFSET,
    MAX_SLICE_ROWS,
    RESULT_META_KEY,
    SMALL_PROJECTION_BYTES,
    SMALL_PROJECTION_ROWS,
    SliceRefused,
    build_result_meta,
    decode_cursor,
    encode_cursor,
    read_manifest,
    read_slice,
    validate_result_meta,
)

_CORE = Path(result_slices.__file__).parent
_SOURCE = Path(result_slices.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The four STRUCTURAL assertions. These are the ones that survive a future edit.
# ---------------------------------------------------------------------------

_FORBIDDEN_IMPORTS = {
    "core.warehouse",
    "core.warehouse_write",
    "core.cache_warehouse",
    "core.bigquery_raw_writer",
    "core.semantic_compiler",
}


def _imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_a_the_warehouse_is_absent_from_the_import_graph():
    """`core.warehouse` is the single door to a warehouse on the Analyze path.

    `core/query_execution.py` imports it inside `run_execution` and picks
    `_query_bigquery` or `_query_duckdb`. A module that cannot import it cannot
    reach a warehouse, whatever its arguments say -- so the escape hatch is not
    forbidden by policy, it is missing from the graph.
    """
    imported = _imported_modules(_SOURCE)
    assert not (imported & _FORBIDDEN_IMPORTS), sorted(imported & _FORBIDDEN_IMPORTS)


def test_b_it_neither_imports_nor_redefines_build_sql():
    """Compiling a query is Story 50.1's job, once, at execution.

    A slice read happens AFTER the answer is frozen. A reader that could compose
    SQL would be a second analytical path wearing a pagination costume.
    """
    tree = ast.parse(_SOURCE)
    # Inspected as CODE, not as text: the module docstring names `build_sql` on
    # purpose, to say why it is absent. A prose mention is documentation; a Name,
    # an import or a definition is the defect.
    assert "build_sql" not in _imported_modules(_SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert "build_sql" not in {alias.name for alias in node.names}
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name != "build_sql"
        if isinstance(node, ast.Name):
            assert node.id != "build_sql"
        if isinstance(node, ast.Attribute):
            assert node.attr != "build_sql"


def _string_literals(source: str) -> list[str]:
    """Every string CONSTANT in the module, with docstrings excluded.

    Collected by walking the AST rather than by matching quote characters in the
    source. A regex only sees the quoting styles whoever wrote it happened to
    think of, and a literal it cannot see is a literal the guard does not
    guard: the previous expression collected triple-quoted and double-quoted
    strings only, so a single-quoted ``'DELETE FROM ...'`` was invisible and the
    "no write verb, exactly one SELECT" property was evadable by changing a
    quote character. `ast.Constant` has no quoting style.

    Docstrings are excluded, and that exclusion is deliberate rather than
    convenient: this module DOCUMENTS the statements it must not contain -- its
    prose names `build_sql`, the single SELECT and the absent write verbs on
    purpose, to say why they are absent. A docstring is documentation; a string
    used as a value is code, and only code can reach a cursor. f-string parts
    are `ast.Constant` nodes too, so a single-quoted f-string does not escape
    either.
    """
    tree = ast.parse(source)
    docstring_nodes: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstring_nodes.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_nodes
    ]


def test_c_it_contains_no_write_statement_and_no_string_built_sql():
    """AC7: a slice read cannot alter the evidence it reads, even if asked to."""
    sql_like = _string_literals(_SOURCE)
    for literal in sql_like:
        upper = literal.upper()
        for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
            assert verb not in upper, f"write statement in result_slices: {literal[:80]}"
    # Exactly one SELECT, and it names the frozen payload table.
    selects = [lit for lit in sql_like if "SELECT" in lit.upper()]
    assert len(selects) == 1, selects
    assert "app.query_result_payloads" in selects[0]
    assert "%s" in selects[0]

    # No f-string and no %-formatting reaching a cursor.
    tree = ast.parse(_SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "execute":
            first = node.args[0] if node.args else None
            assert not isinstance(first, ast.JoinedStr), "f-string reaching a cursor"
            assert not (
                isinstance(first, ast.BinOp) and isinstance(first.op, ast.Mod)
            ), "%-formatted SQL reaching a cursor"


def test_d_the_result_meta_key_is_written_in_exactly_one_file():
    """AC5: no second `_meta` builder can appear beside the denylist tripwire.

    The tripwire catches a mistake INSIDE the one allowed builder. It only means
    something because there is nowhere else to make the mistake.
    """
    writers = [
        path
        for path in _CORE.glob("*.py")
        if '"toorow.result"' in path.read_text(encoding="utf-8")
        or "'toorow.result'" in path.read_text(encoding="utf-8")
    ]
    assert [p.name for p in writers] == ["result_slices.py"], [p.name for p in writers]


def test_build_result_meta_accepts_no_channel_through_which_a_secret_could_arrive():
    """The signature IS the guarantee (AC5).

    No connection, no token, no request context, no `**kwargs`, no free-form dict.
    Every value that can leave carries frozen Result evidence, an opaque handle or
    an integer.
    """
    import inspect

    sig = inspect.signature(build_result_meta)
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in sig.parameters.values())
    assert set(sig.parameters) == {
        "result_id",
        "content_hash",
        "result_schema",
        "manifest",
        "rows_chunk",
        "allowed_columns",
        "row_count",
        "truncated",
        "result_handle",
        # Added 2026-08-07: the Result's AI Path walk, projected by the owner's
        # wire shape (`core.ai_paths.wire_step_projection`) -- frozen recorded
        # evidence like everything else on this list, and still no channel
        # through which a credential could arrive.
        "ai_path_walk",
    }
    assert not [
        p
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
    ]


# ---------------------------------------------------------------------------
# Fakes -- a connection that answers the one SELECT, and an access decision.
# ---------------------------------------------------------------------------

_HASH = "a" * 64
_SCHEMA = {"fields": [{"name": "day"}, {"name": "clicks"}, {"name": "cost"}]}


class _Cursor:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _Cursor(self._row)


def _payload_conn(rows, content_hash=_HASH, schema=None):
    return _Conn((content_hash, schema or _SCHEMA, {"note": "manifest"}, rows))


class _Decision:
    def __init__(self, allowed=True, org_id="org_EXAMPLE"):
        self.allowed = allowed
        self.org_id = org_id


def _rows(n):
    return [{"day": f"2026-07-{i:02d}", "clicks": i, "cost": i * 2} for i in range(1, n + 1)]


def _grant(**overrides):
    grant = {
        "handle_id": "rh_" + "0" * 26,
        "org_id": "org_EXAMPLE",
        "project_id": "proj_EXAMPLE",
        "result_id": "qr_EXAMPLE",
        "content_hash": _HASH,
        "issued_to_identity": "owner@example.com",
        "allowed_columns": ["day", "clicks", "cost"],
        "issued_at": None,
        "last_read_at": None,
        "expires_at": None,
        "revoked_at": None,
    }
    grant.update(overrides)
    return grant


@pytest.fixture
def wired(monkeypatch):
    """Wire the two lazily-imported seams: the access decision and the grant load."""
    state = {"decision": _Decision(), "grant": _grant(), "live": True}

    import core.project_access as project_access
    import core.result_app_grants as grants

    monkeypatch.setattr(
        project_access, "resolve_strict_resource_access", lambda *a, **k: state["decision"]
    )
    monkeypatch.setattr(grants, "load_grant", lambda conn, **k: state["grant"])
    monkeypatch.setattr(grants, "grant_is_live", lambda g, **k: state["live"])
    return state


def _read(conn, **kwargs):
    params = {
        "handle_id": "rh_" + "0" * 26,
        "identity": "owner@example.com",
        "project_id": "proj_EXAMPLE",
    }
    params.update(kwargs)
    return read_slice(conn, **params)


# ---------------------------------------------------------------------------
# AC6 property 2 -- the grant can only narrow; it can never grant.
# ---------------------------------------------------------------------------


def test_a_valid_handle_presented_by_a_denied_identity_returns_not_found(wired):
    """The whole "not a credential" claim reduces to this test."""
    wired["decision"] = _Decision(allowed=False, org_id=None)
    with pytest.raises(SliceRefused) as excinfo:
        _read(_payload_conn(_rows(3)))
    assert excinfo.value.code == "not_found"


def test_a_guard_that_raises_fails_closed(wired, monkeypatch):
    import core.project_access as project_access

    def boom(*a, **k):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(project_access, "resolve_strict_resource_access", boom)
    with pytest.raises(SliceRefused) as excinfo:
        _read(_payload_conn(_rows(3)))
    assert excinfo.value.code == "not_found"


# ---------------------------------------------------------------------------
# AC6 property 3 -- absent, foreign, revoked and expired are indistinguishable.
# ---------------------------------------------------------------------------


def test_absent_foreign_revoked_and_expired_return_byte_identical_envelopes(wired):
    envelopes = []
    cases = [
        ("absent", lambda: wired.__setitem__("grant", None)),
        ("foreign_identity", lambda: wired.__setitem__("grant", _grant(issued_to_identity="x"))),
        ("foreign_org", lambda: wired.__setitem__("grant", _grant(org_id="org_OTHER"))),
        ("revoked_or_expired", lambda: wired.__setitem__("live", False)),
    ]
    for _name, arrange in cases:
        wired["grant"] = _grant()
        wired["live"] = True
        arrange()
        with pytest.raises(SliceRefused) as excinfo:
            _read(_payload_conn(_rows(3)))
        envelopes.append(excinfo.value.as_dict())
    assert all(env == envelopes[0] for env in envelopes), envelopes
    assert envelopes[0] == {"code": "not_found", "message": "Not found"}


def test_a_malformed_handle_is_nondisclosing_and_never_loaded(wired, monkeypatch):
    import core.result_app_grants as grants

    monkeypatch.setattr(
        grants,
        "load_grant",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not load grant")),
    )
    with pytest.raises(SliceRefused) as excinfo:
        _read(_payload_conn(_rows(1)), handle_id="rh_not-a-handle")
    assert excinfo.value.as_dict() == {"code": "not_found", "message": "Not found"}


# ---------------------------------------------------------------------------
# AC7 -- bounded on every axis, and refusing rather than reducing.
# ---------------------------------------------------------------------------


def test_an_over_bound_limit_is_refused_never_silently_reduced(wired):
    with pytest.raises(SliceRefused) as excinfo:
        _read(_payload_conn(_rows(10)), limit=MAX_SLICE_ROWS + 1)
    detail = excinfo.value.as_dict()
    assert detail["code"] == "limit_over_bound"
    assert detail["requested"] == MAX_SLICE_ROWS + 1
    assert detail["bound"] == MAX_SLICE_ROWS


def test_declared_row_column_offset_and_cursor_bounds_are_inclusive(wired):
    page = _read(_payload_conn(_rows(MAX_SLICE_ROWS)), limit=MAX_SLICE_ROWS)
    assert page["returned_rows"] == MAX_SLICE_ROWS

    columns = [f"column_{index}" for index in range(MAX_SLICE_COLUMNS)]
    schema = {"fields": [{"name": column} for column in columns]}
    wired["grant"] = _grant(allowed_columns=columns)
    row = {column: index for index, column in enumerate(columns)}
    assert _read(_payload_conn([row], schema=schema))["columns"] == columns

    wired["grant"] = _grant()
    assert _read(_payload_conn(_rows(1)), offset=MAX_SLICE_OFFSET)["rows"] == []
    cursor = encode_cursor(
        result_id="qr_EXAMPLE", content_hash=_HASH, offset=MAX_SLICE_OFFSET
    )
    assert decode_cursor(cursor)["offset"] == MAX_SLICE_OFFSET

    exact_cursor = None
    for result_id_length in range(1, 600):
        try:
            candidate = encode_cursor(
                result_id="q" * result_id_length,
                content_hash=_HASH,
                offset=MAX_SLICE_OFFSET,
            )
        except SliceRefused:
            break
        if len(candidate.encode("ascii")) == MAX_CURSOR_BYTES:
            exact_cursor = candidate
            break
    assert exact_cursor is not None
    assert len(exact_cursor.encode("ascii")) == MAX_CURSOR_BYTES
    assert decode_cursor(exact_cursor)["offset"] == MAX_SLICE_OFFSET


def test_more_than_the_column_bound_is_refused_never_silently_reduced(wired):
    columns = [f"column_{index}" for index in range(MAX_SLICE_COLUMNS + 1)]
    schema = {"fields": [{"name": column} for column in columns]}
    wired["grant"] = _grant(allowed_columns=columns)
    with pytest.raises(SliceRefused) as excinfo:
        row = {column: index for index, column in enumerate(columns)}
        _read(_payload_conn([row], schema=schema))
    detail = excinfo.value.as_dict()
    assert detail["code"] == "columns_over_bound"
    assert detail["requested"] == MAX_SLICE_COLUMNS + 1
    assert detail["bound"] == MAX_SLICE_COLUMNS


def test_a_zero_or_negative_limit_is_refused(wired):
    for bad in (0, -1):
        with pytest.raises(SliceRefused) as excinfo:
            _read(_payload_conn(_rows(10)), limit=bad)
        assert excinfo.value.code == "limit_over_bound"


def test_cursor_and_offset_are_mutually_exclusive(wired):
    cursor = encode_cursor(result_id="qr_EXAMPLE", content_hash=_HASH, offset=0)
    with pytest.raises(SliceRefused) as excinfo:
        _read(_payload_conn(_rows(10)), cursor=cursor, offset=0)
    assert excinfo.value.code == "cursor_and_offset_both_supplied"


def test_an_offset_past_the_end_returns_an_empty_page_not_an_error(wired):
    """An error here would leak the true row count to a caller probing for it."""
    page = _read(_payload_conn(_rows(10)), offset=999)
    assert page["rows"] == []
    assert page["has_more"] is False
    assert page["next_cursor"] is None


def test_a_negative_offset_is_refused(wired):
    with pytest.raises(SliceRefused) as excinfo:
        _read(_payload_conn(_rows(10)), offset=-1)
    assert excinfo.value.code == "offset_out_of_bounds"


def test_an_offset_above_the_frozen_storage_bound_is_refused(wired):
    with pytest.raises(SliceRefused) as excinfo:
        _read(_payload_conn(_rows(10)), offset=MAX_SLICE_OFFSET + 1)
    assert excinfo.value.code == "offset_out_of_bounds"


def test_an_oversized_cursor_is_refused_before_decode():
    with pytest.raises(SliceRefused) as excinfo:
        decode_cursor("a" * (MAX_CURSOR_BYTES + 1))
    assert excinfo.value.code == "cursor_invalid"


def test_the_byte_budget_refusal_names_the_count_and_the_budget(wired, monkeypatch):
    monkeypatch.setattr(result_slices, "MAX_SLICE_BYTES", 200)
    with pytest.raises(SliceRefused) as excinfo:
        _read(_payload_conn(_rows(50)), limit=50)
    detail = excinfo.value.as_dict()
    assert detail["code"] == "slice_over_byte_budget"
    assert detail["budget"] == 200
    assert detail["measured"] > 200


def test_the_slice_byte_budget_accepts_exactly_the_measured_size(wired, monkeypatch):
    page = _read(_payload_conn(_rows(3)), limit=3)
    measured = len(json.dumps(page, default=str, separators=(",", ":")).encode("utf-8"))
    monkeypatch.setattr(result_slices, "MAX_SLICE_BYTES", measured)
    assert _read(_payload_conn(_rows(3)), limit=3) == page
    monkeypatch.setattr(result_slices, "MAX_SLICE_BYTES", measured - 1)
    with pytest.raises(SliceRefused) as excinfo:
        _read(_payload_conn(_rows(3)), limit=3)
    assert excinfo.value.code == "slice_over_byte_budget"


def test_paging_walks_the_frozen_rows_in_order(wired):
    conn = _payload_conn(_rows(5))
    first = _read(conn, limit=2)
    assert [r["clicks"] for r in first["rows"]] == [1, 2]
    assert first["has_more"] is True
    second = _read(conn, cursor=first["next_cursor"], limit=2)
    assert [r["clicks"] for r in second["rows"]] == [3, 4]
    third = _read(conn, cursor=second["next_cursor"], limit=2)
    assert [r["clicks"] for r in third["rows"]] == [5]
    assert third["has_more"] is False
    assert third["next_cursor"] is None


# ---------------------------------------------------------------------------
# AC6 properties 5 and 6 -- allowlist and pinned hash.
# ---------------------------------------------------------------------------


def test_an_unknown_column_is_refused_never_silently_dropped(wired):
    with pytest.raises(SliceRefused) as excinfo:
        _read(_payload_conn(_rows(3)), columns=["day", "secret_salary"])
    detail = excinfo.value.as_dict()
    assert detail["code"] == "column_not_in_result_schema"
    assert detail["columns"] == ["secret_salary"]


def test_a_grant_narrows_the_columns_and_cannot_widen_them(wired):
    wired["grant"] = _grant(allowed_columns=["day"])
    page = _read(_payload_conn(_rows(3)))
    assert page["columns"] == ["day"]
    assert set(page["rows"][0]) == {"day"}
    # `cost` is in the Result schema but NOT in this grant: asking is refused.
    with pytest.raises(SliceRefused) as excinfo:
        _read(_payload_conn(_rows(3)), columns=["cost"])
    assert excinfo.value.code == "column_not_in_result_schema"


def test_positional_rows_are_projected_against_the_full_frozen_schema(wired):
    wired["grant"] = _grant(allowed_columns=["cost"])
    page = _read(_payload_conn([["2026-07-01", 7, 11]]))
    assert page["rows"] == [{"cost": 11}]


def test_a_scalar_frozen_row_is_refused_instead_of_emitting_a_stalled_cursor(wired):
    with pytest.raises(SliceRefused) as excinfo:
        _read(_payload_conn(["not-a-row"]), limit=1)
    assert excinfo.value.code == "result_changed"


def test_a_content_hash_mismatch_refuses_with_result_changed(wired):
    with pytest.raises(SliceRefused) as excinfo:
        _read(_payload_conn(_rows(3), content_hash="b" * 64))
    assert excinfo.value.code == "result_changed"


def test_a_cursor_from_another_result_fails_loudly(wired):
    other = encode_cursor(result_id="qr_OTHER", content_hash=_HASH, offset=0)
    with pytest.raises(SliceRefused) as excinfo:
        _read(_payload_conn(_rows(3)), cursor=other)
    assert excinfo.value.code == "cursor_result_mismatch"


def test_a_malformed_cursor_is_refused():
    with pytest.raises(SliceRefused) as excinfo:
        decode_cursor("not-a-cursor")
    assert excinfo.value.code == "cursor_invalid"


def test_the_manifest_read_obeys_the_same_guard(wired):
    wired["decision"] = _Decision(allowed=False, org_id=None)
    with pytest.raises(SliceRefused) as excinfo:
        read_manifest(
            _payload_conn(_rows(3)),
            handle_id="rh_" + "0" * 26,
            identity="owner@example.com",
            project_id="proj_EXAMPLE",
        )
    assert excinfo.value.code == "not_found"


def test_the_manifest_read_returns_the_frozen_schema_and_manifest(wired):
    out = read_manifest(
        _payload_conn(_rows(3)),
        handle_id="rh_" + "0" * 26,
        identity="owner@example.com",
        project_id="proj_EXAMPLE",
    )
    assert out["schema"] == _SCHEMA
    assert out["manifest"] == {}
    assert out["allowed_columns"] == ["day", "clicks", "cost"]


def test_manifest_reads_reuse_the_safe_renderer_projection(wired):
    conn = _Conn(
        (
            _HASH,
            _SCHEMA,
            {
                "grain": "day",
                "relation": "secret_physical_table",
                "options": {"renderer": "unsafe"},
                "missing_link": "../../internal",
            },
            _rows(1),
        )
    )
    out = read_manifest(
        conn,
        handle_id="rh_" + "0" * 26,
        identity="owner@example.com",
        project_id="proj_EXAMPLE",
    )
    assert out["manifest"] == {"grain": "day", "missing_link": "source_output"}


def test_manifest_reads_refuse_their_transport_byte_bound(wired, monkeypatch):
    monkeypatch.setattr(result_slices, "MAX_MANIFEST_BYTES", 200)
    conn = _Conn((_HASH, _SCHEMA, {"filters": ["x" * MAX_MANIFEST_BYTES]}, _rows(1)))
    with pytest.raises(SliceRefused) as excinfo:
        read_manifest(
            conn,
            handle_id="rh_" + "0" * 26,
            identity="owner@example.com",
            project_id="proj_EXAMPLE",
        )
    detail = excinfo.value.as_dict()
    assert detail["code"] == "manifest_over_byte_budget"
    assert detail["budget"] == 200


# ---------------------------------------------------------------------------
# AC5/AC6 -- the small/large branch, at the declared threshold.
# ---------------------------------------------------------------------------


def _meta(n_rows, **kwargs):
    params = {
        "result_id": "qr_EXAMPLE",
        "content_hash": _HASH,
        "result_schema": _SCHEMA,
        "manifest": {"note": "manifest"},
        "rows_chunk": _rows(n_rows),
        "allowed_columns": ["day", "clicks", "cost"],
        "row_count": n_rows,
        "truncated": False,
        "result_handle": "rh_" + "0" * 26,
    }
    params.update(kwargs)
    return build_result_meta(**params)[RESULT_META_KEY]


def test_a_small_projection_carries_its_rows_inline():
    meta = _meta(SMALL_PROJECTION_ROWS)
    assert meta["projection_size"] == "small"
    assert len(meta["rows"]) == SMALL_PROJECTION_ROWS
    assert meta["schema"] == _SCHEMA


def test_a_large_result_carries_NO_rows_only_a_handle_manifest_and_projection():
    meta = _meta(SMALL_PROJECTION_ROWS + 1)
    assert meta["projection_size"] == "large"
    assert "rows" not in meta
    assert meta["result_handle"].startswith("rh_")
    assert meta["manifest"] == {}
    assert len(meta["initial_projection"]) == SMALL_PROJECTION_ROWS
    assert meta["next_cursor"] is not None


def test_a_byte_large_result_gets_a_row_and_byte_bounded_prefix():
    rows = [{"day": "2026-07-01", "clicks": "x" * 40_000, "cost": 1} for _ in range(3)]
    meta = _meta(0, rows_chunk=rows, row_count=3)
    encoded = json.dumps(meta["initial_projection"], separators=(",", ":")).encode("utf-8")
    assert meta["projection_size"] == "large"
    assert len(meta["initial_projection"]) == 1
    assert len(encoded) <= SMALL_PROJECTION_BYTES
    assert decode_cursor(meta["next_cursor"])["offset"] == 1


def test_the_initial_projection_byte_boundary_is_inclusive_then_switches():
    empty = [{"day": "", "clicks": 1, "cost": 1}]
    overhead = len(json.dumps(empty, separators=(",", ":")).encode("utf-8"))
    exact_row = {"day": "x" * (SMALL_PROJECTION_BYTES - overhead), "clicks": 1, "cost": 1}
    assert (
        len(json.dumps([exact_row], separators=(",", ":")).encode("utf-8"))
        == SMALL_PROJECTION_BYTES
    )
    exact = _meta(0, rows_chunk=[exact_row], row_count=1)
    assert exact["projection_size"] == "small"
    assert exact["rows"] == [exact_row]

    over_row = {**exact_row, "day": exact_row["day"] + "x"}
    over = _meta(0, rows_chunk=[over_row], row_count=1)
    assert over["projection_size"] == "large"
    assert over["initial_projection"] == []


def test_a_single_row_over_the_prefix_budget_is_not_cut():
    rows = [{"day": "2026-07-01", "clicks": "x" * SMALL_PROJECTION_BYTES, "cost": 1}]
    meta = _meta(0, rows_chunk=rows, row_count=1)
    assert meta["initial_projection"] == []
    assert decode_cursor(meta["next_cursor"])["offset"] == 0


def test_a_large_result_without_a_live_handle_is_refused():
    with pytest.raises(SliceRefused) as excinfo:
        _meta(SMALL_PROJECTION_ROWS + 1, result_handle=None)
    assert excinfo.value.code == "large_result_requires_handle"


def test_the_branch_switches_exactly_at_the_declared_threshold():
    assert _meta(SMALL_PROJECTION_ROWS)["projection_size"] == "small"
    assert _meta(SMALL_PROJECTION_ROWS + 1)["projection_size"] == "large"


def test_the_meta_projects_only_allowlisted_columns():
    meta = _meta(3, allowed_columns=["day"])
    assert set(meta["rows"][0]) == {"day"}


def test_the_meta_reports_the_exact_counts_from_the_result():
    meta = _meta(4, row_count=4, truncated=True)
    assert meta["row_count"] == 4
    assert meta["truncated"] is True


def test_the_meta_is_written_under_exactly_one_namespaced_key():
    built = build_result_meta(
        result_id="qr_EXAMPLE",
        content_hash=_HASH,
        result_schema=_SCHEMA,
        manifest={},
        rows_chunk=[],
        allowed_columns=["day"],
        row_count=0,
        truncated=False,
        result_handle=None,
    )
    assert list(built) == [RESULT_META_KEY]


def test_the_final_meta_budget_includes_every_additive_sidecar(monkeypatch):
    aggregate = {
        RESULT_META_KEY: _meta(1),
        "toorow.feedback": {"schema_version": "exact-feedback.v1", "token": "signed"},
    }
    measured = len(json.dumps(aggregate, default=str, separators=(",", ":")).encode())
    monkeypatch.setattr(result_slices, "MAX_RESULT_META_BYTES", measured)
    assert validate_result_meta(aggregate) == aggregate
    monkeypatch.setattr(result_slices, "MAX_RESULT_META_BYTES", measured - 1)
    with pytest.raises(SliceRefused) as excinfo:
        validate_result_meta(aggregate)
    assert excinfo.value.code == "result_meta_over_byte_budget"
    assert excinfo.value.as_dict()["measured"] == measured
