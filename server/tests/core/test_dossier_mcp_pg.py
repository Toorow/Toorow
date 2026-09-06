"""Story 74-1 -- `compose_dossier`, the TOOL, driven through a FastMCP client on a real Postgres.

WHY THE TOOL AND NOT THE FUNCTION. AI-345 measured what a function test proves
about an MCP door: nothing about the door. The render tool refused its own spec
in production while `compose_render_app_payload` was green. These tests call
`compose_dossier` by name, through `Client(FastMCPTransport(...))`, so the
argument shapes, the ToolError envelopes and the `structured_content` a host
receives are the ones a host receives.

WHY A REAL DATABASE. Every property below is a row, a CHECK or a transaction:
one `app.renders` row per figure, `ck_renders_pins_are_exact` satisfied by pins
the SERVER resolved, `ck_renders_creation_surface` admitting `mcp`, a dossier
version pinned to those exact Renders, and -- the load-bearing one -- a figure
refused half-way leaving NO Render and NO Dossier behind. A mocked cursor accepts
all of that happily.

Every test rolls back: the tool's own `conn.commit()` lands on a proxy whose
commit is a no-op, so the assertions read uncommitted rows on the same
connection and the fixture rolls them back.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from typing import Any

import pytest
from core import dossier_mcp
from core.dossiers import get_dossier
from fastmcp import Client, FastMCP
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import ToolError

from tests.core.test_analyze_artifacts_pg import (
    _HASH_B,
    Chain,
    _visualization_spec_version,
)
from tests.core.test_render_pins_are_named_pg import _ADAPTERLESS_PINS, _register_build

pytestmark = [pytest.mark.usefixtures("live_postgres"), pytest.mark.anyio]

IDENTITY = "owner@example.com"
RENDERER_BUILD = "table/toorow-table@1.0.0"
RENDERER_ID = "toorow-table"
NARRATIVE = "Views doubled in the second week; the two new uploads carry the growth."

# What `mcp_profiles._capability_context` returns for a host whose live
# `app.mcp_capability_contexts` row enables `operations`, is bound to an
# endpoint/workspace, and carries server-minted interactive presence. Every field
# below is READ by a guard: `attested_context_id` by `context_attested`,
# `endpoint_binding` + `workspace_evidence_hash` by `_endpoint_workspace_verified`,
# `interactive_presence_evidence_hash` by `interactive_presence_verified` (a
# `confirmed_write` is hidden without it). Nothing here is a host self-report.
ATTESTED_CONTEXT: tuple[str, dict[str, Any], dict[str, Any]] = (
    IDENTITY,
    {"host": "opaque", "workspace_id": "ws_EXAMPLE", "workspace_type": "desktop"},
    {
        "attested_context_id": "mcx_EXAMPLE",
        "enabled_profiles": ["insights", "operations"],
        "endpoint_binding": "endpoint.example.com",
        "workspace_evidence_hash": "b" * 64,
        "interactive_presence_evidence_hash": "c" * 64,
    },
)


class _NoCommit:
    """The fixture's connection, with `commit()` swallowed so the test can roll back."""

    def __init__(self, conn):
        self._conn = conn

    def commit(self) -> None:
        return None

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def _seed_payload(chain: Chain) -> None:
    """The retained payload `_load_payload` reads -- Chain seeds the Result only."""
    schema = {
        "fields": [
            {"id": "sc_EXAMPLE_DAY", "name": "day"},
            {"id": "sc_EXAMPLE_VIEWS", "name": "views"},
        ]
    }
    rows = [{"day": "2026-08-01", "views": 10}, {"day": "2026-08-02", "views": 20}]
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.query_result_payloads
                (result_id, org_id, project_id, content_hash, result_schema, manifest, rows_chunk)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
            """,
            (
                chain.result_id,
                chain.org_id,
                chain.project_id,
                _HASH_B,
                json.dumps(schema),
                json.dumps({"grain": "day"}),
                json.dumps(rows),
            ),
        )


def _write_runtime(tmp_path) -> str:
    """A delivered runtime whose identities are the ledger row's, so the adapter derives."""
    bundle = tmp_path / "mcp-app.html"
    bundle.write_text('<!doctype html><div id="root"></div>', encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        "runtime_build": _ADAPTERLESS_PINS["runtime_build_id"],
        "theme_version": _ADAPTERLESS_PINS["theme_version"],
        "formatter_version": _ADAPTERLESS_PINS["formatter_version"],
        "renderers": {
            "table": {
                "renderer_build": RENDERER_BUILD,
                "schema_versions": {"min": 1, "max": 1},
                "profiles": [dossier_mcp.FIGURE_PROFILE, "share"],
            }
        },
    }
    bundle.with_name("runtime-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return str(bundle)


@pytest.fixture()
def chain(live_postgres):
    built = Chain(live_postgres).build()
    _seed_payload(built)
    _register_build(built, build_id=RENDERER_BUILD, renderer_id=RENDERER_ID)
    yield built
    live_postgres.rollback()


@pytest.fixture()
def spec_version_id(chain) -> str:
    return _visualization_spec_version(chain)


@pytest.fixture()
def wired(chain, monkeypatch, tmp_path):
    """The real tool UNDER THE PROFILED SURFACE'S OWN MIDDLEWARE, on a real connection.

    The fixture used to build a bare `FastMCP` with no middleware at all, so the
    door these tests drove was not the door a host reaches: `core/main.py:426`
    attaches `CapabilityProfileMiddleware` (`core/mcp_profiles.py:1025`) to the
    assembled server, and that middleware is where the model-channel budget and
    the `evidence_required_with_deep_link` refusal live. G15-T03 was refused on
    the deployment on 2026-09-04 while these 36 tests were green -- exactly the
    blind spot clause 45 of the 2026-09-02 amendment of
    `docs/product-architecture/visualization-and-rendering.md` names ("a test of
    the tool calls it without the model-channel guard the profiled surface
    applies").

    The capability context is the other half: `compose_dossier` is declared
    `operations` / `confirmed_write`, so the middleware hides it from an
    unattested caller. `_capability_context` normally reads
    `app.mcp_capability_contexts` at every call; here it is stubbed with the
    shape that row produces for an attested, endpoint-bound, interactively
    present host -- the ONLY shape under which a host may call this tool.
    """
    import core.audit
    import core.db
    import core.main
    from core import mcp_profiles

    monkeypatch.setenv("TOOROW_VISUALIZATION_RUNTIME_DIST", _write_runtime(tmp_path))
    monkeypatch.setattr(dossier_mcp, "_identity", lambda: IDENTITY)
    monkeypatch.setattr(core.main, "_resolve_project", lambda pid, identity=None: pid)
    monkeypatch.setattr(dossier_mcp, "refuse_unless_project_scope", lambda *a, **k: None)
    audit_rows: list[dict[str, Any]] = []
    monkeypatch.setattr(core.audit, "write_audit_row", lambda **kw: audit_rows.append(kw))

    @contextmanager
    def _request_connection(identity):
        # Production's `request_connection` discards uncommitted work when the
        # body raises; here the same fact is a SAVEPOINT (`conn.transaction()`
        # inside the fixture's open transaction), so a refused call rolls back
        # the tool's rows and keeps the seeded chain for the assertions.
        assert identity == IDENTITY
        with chain.conn.transaction():
            yield _NoCommit(chain.conn)

    monkeypatch.setattr(core.db, "request_connection", _request_connection)
    monkeypatch.setattr(mcp_profiles, "_capability_context", lambda: ATTESTED_CONTEXT)
    target = FastMCP("story-74-1")
    dossier_mcp.register(target)
    middleware = mcp_profiles.build_middleware()
    assert middleware is not None, "the profiled surface's middleware must be attachable"
    target.add_middleware(middleware)
    return target, audit_rows


async def _compose(target: FastMCP, **arguments):
    async with Client(FastMCPTransport(target)) as client:
        return await client.call_tool("compose_dossier", arguments)


def _envelope(exc: ToolError) -> dict[str, Any]:
    text = str(exc)
    return json.loads(text[text.index("{") : text.rindex("}") + 1])


def _figure(chain, spec_version_id: str) -> dict[str, Any]:
    return {
        "kind": "figure",
        "result_id": chain.result_id,
        "visualization_spec_version_id": spec_version_id,
    }


def _count(conn, table: str, project_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id = %s", (project_id,))  # noqa: S608
        return int(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# The fourth step, held end to end on the row level.
# ---------------------------------------------------------------------------


async def test_the_tool_freezes_one_render_per_figure_and_keeps_the_narrative_as_the_models(
    chain, spec_version_id, wired
):
    target, audit_rows = wired
    result = await _compose(
        target,
        project_id=chain.project_id,
        label="August videos",
        blocks=[_figure(chain, spec_version_id), {"kind": "narrative", "text": NARRATIVE}],
    )

    structured = result.structured_content
    assert structured["dossier_id"].startswith("dos_")
    assert structured["version_number"] == 1
    assert len(structured["render_ids"]) == 1 and structured["render_ids"][0].startswith("rnd_")
    assert structured["narrative_blocks"] == 1
    assert structured["deep_link"]["object_type"] == "dossier"
    assert structured["deep_link"]["object_id"] == structured["dossier_id"]
    assert structured["deep_link"]["requires_authenticated_session"] is True

    # The answer must pass the SAME model-channel guard the profiled surface
    # applies (`mcp_profiles` -> `model_channel.enforce_model_channel`): a deep
    # link beside bounded evidence, never instead of it. Measured 2026-09-04:
    # the deployment refused G15-T03 `evidence_required_with_deep_link` while
    # this test, calling the tool without the wrapper, stayed green.
    from core import model_channel

    model_visible, _app_payload = model_channel.partition_envelope(
        structured, tool_name="compose_dossier"
    )
    model_channel.enforce_model_channel("compose_dossier", result.content, model_visible)
    assert {row["column"] for row in structured["evidence"]} >= {
        "dossier_version_id",
        "render_ids",
        "narrative_blocks",
    }

    # The Render is a real row, frozen under pins the SERVER resolved, on the
    # `mcp` creation surface, and the adapter was derived from the ledger.
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            SELECT result_id, visualization_spec_version_id, renderer_build_id, runtime_build_id,
                   renderer_adapter, responsive_profile, creation_surface, origin_kind,
                   evidence_manifest, created_by
            FROM app.renders WHERE id = %s
            """,
            (structured["render_ids"][0],),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == chain.result_id
    assert row[1] == spec_version_id
    assert row[2] == RENDERER_BUILD
    assert row[3] == _ADAPTERLESS_PINS["runtime_build_id"]
    assert row[4] == RENDERER_ID
    assert row[5] == dossier_mcp.FIGURE_PROFILE
    assert row[6] == "mcp"
    assert row[7] == "explore"
    assert row[8]["composed_by"] == "compose_dossier"
    assert row[8]["query_spec_version_id"] == chain.query_spec_version_id
    assert row[9] == IDENTITY

    # The Dossier pins that exact Render, in order, and the narrative says who wrote it.
    dossier = get_dossier(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        dossier_id=structured["dossier_id"],
    )
    blocks = dossier["current_resolved_blocks"]
    assert [b["kind"] for b in blocks] == ["render", "narrative"]
    assert blocks[0]["render_id"] == structured["render_ids"][0]
    assert blocks[0]["provenance"]["result_id"] == chain.result_id
    assert blocks[0]["provenance_missing"] is False
    assert "ai_path" in blocks[0]["provenance"]  # resolved off the Result, None here
    assert blocks[1] == {"kind": "narrative", "text": NARRATIVE, "authored_by": "model"}

    # The text names the identities; nothing in the answer is a share link.
    text = result.content[0].text
    assert structured["dossier_id"] in text and structured["render_ids"][0] in text
    whole = json.dumps({"text": text, "structured": structured})
    assert "http" not in whole and "#render=" not in whole

    assert audit_rows and audit_rows[0]["action"] == dossier_mcp.ACTION_DOSSIER_COMPOSED
    assert audit_rows[0]["metadata"]["render_ids"] == structured["render_ids"]


async def test_a_caller_cannot_claim_a_person_wrote_the_narrative(chain, spec_version_id, wired):
    """D3: through this door every narrative is the model's, whatever the block says."""
    target, _ = wired
    result = await _compose(
        target,
        project_id=chain.project_id,
        label="Claimed",
        blocks=[
            {"kind": "narrative", "text": NARRATIVE, "authored_by": "human"},
            _figure(chain, spec_version_id),
        ],
    )
    dossier = get_dossier(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        dossier_id=result.structured_content["dossier_id"],
    )
    assert dossier["current_resolved_blocks"][0]["authored_by"] == "model"


async def test_a_refused_figure_leaves_no_render_and_no_dossier(chain, spec_version_id, wired):
    """One transaction: the first figure froze, the second is foreign, nothing survives."""
    target, audit_rows = wired
    before = (
        _count(chain.conn, "app.renders", chain.project_id),
        _count(chain.conn, "app.analysis_dossiers", chain.project_id),
    )
    with pytest.raises(ToolError) as caught:
        await _compose(
            target,
            project_id=chain.project_id,
            label="Half a dossier",
            blocks=[
                _figure(chain, spec_version_id),
                {
                    "kind": "figure",
                    "result_id": "qr_ELSEWHERE",
                    "visualization_spec_version_id": spec_version_id,
                },
            ],
        )
    envelope = _envelope(caught.value)
    assert envelope["code"] == "result_not_found"
    assert envelope["refusals"][0]["subject"] == "blocks[1]"
    # Psycopg leaves the transaction aborted after nothing -- the tool raised
    # before any statement failed -- so the same connection can still count.
    after = (
        _count(chain.conn, "app.renders", chain.project_id),
        _count(chain.conn, "app.analysis_dossiers", chain.project_id),
    )
    assert after == before
    assert audit_rows == []


async def test_an_incompatible_spec_is_refused_by_name_and_writes_nothing(chain, wired):
    """A Spec built for another Query Spec is the render tool's refusal, here too."""
    from tests.core.test_analyze_artifacts_pg import _uid

    target, _ = wired
    other = Chain(chain.conn).build()
    foreign_spec = _visualization_spec_version(other)
    with pytest.raises(ToolError) as caught:
        await _compose(
            target,
            project_id=chain.project_id,
            label="Wrong spec",
            blocks=[
                {
                    "kind": "figure",
                    "result_id": chain.result_id,
                    "visualization_spec_version_id": foreign_spec,
                }
            ],
        )
    # The foreign Spec belongs to another Project, so it is not available HERE.
    assert _envelope(caught.value)["code"] == "visualization_spec_not_available"
    assert _count(chain.conn, "app.renders", chain.project_id) == 0
    assert _uid("x").startswith("x_")  # the helper stays importable for neighbours


async def test_shape_refusals_arrive_all_at_once_before_any_read(chain, wired):
    target, _ = wired
    with pytest.raises(ToolError) as caught:
        await _compose(
            target,
            project_id=chain.project_id,
            label="Malformed",
            blocks=[
                {"kind": "figure"},
                {"kind": "narrative", "text": "   "},
                {
                    "kind": "figure",
                    "result_id": chain.result_id,
                    "visualization_spec_version_id": "vsv_a",
                    "visualization_template_version_id": "vtv_b",
                },
                {"kind": "table"},
            ],
        )
    envelope = _envelope(caught.value)
    assert envelope["code"] == "invalid_blocks"
    assert [r["code"] for r in envelope["refusals"]] == [
        "missing_result_id",
        "empty_narrative",
        "visualization_pin_ambiguous",
        "unknown_block_kind",
    ]
    assert [r["subject"] for r in envelope["refusals"]] == [
        "blocks[0]",
        "blocks[1]",
        "blocks[2]",
        "blocks[3]",
    ]


async def test_a_narrative_alone_is_a_note_and_is_refused(chain, wired):
    target, _ = wired
    with pytest.raises(ToolError) as caught:
        await _compose(
            target,
            project_id=chain.project_id,
            label="Note",
            blocks=[{"kind": "narrative", "text": NARRATIVE}],
        )
    assert _envelope(caught.value)["code"] == "no_figure_block"


async def test_a_second_call_naming_the_dossier_succeeds_its_version(chain, spec_version_id, wired):
    target, _ = wired
    first = await _compose(
        target,
        project_id=chain.project_id,
        label="August videos",
        blocks=[_figure(chain, spec_version_id)],
    )
    second = await _compose(
        target,
        project_id=chain.project_id,
        label="August videos, revised",
        dossier_id=first.structured_content["dossier_id"],
        blocks=[{"kind": "narrative", "text": NARRATIVE}, _figure(chain, spec_version_id)],
    )
    assert second.structured_content["dossier_id"] == first.structured_content["dossier_id"]
    assert second.structured_content["version_number"] == 2
    assert second.structured_content["render_ids"] != first.structured_content["render_ids"]
    dossier = get_dossier(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        dossier_id=first.structured_content["dossier_id"],
    )
    assert dossier["label"] == "August videos, revised"
    assert [v["version_number"] for v in dossier["versions"]] == [2, 1]


# ---------------------------------------------------------------------------
# Clause 45 -- the model-channel guard the profiled surface applies runs HERE.
# ---------------------------------------------------------------------------


async def test_a_deep_link_without_bounded_evidence_is_refused_by_the_profiled_surface(
    chain, spec_version_id, wired, monkeypatch
):
    """Empty the tool's bounded evidence and the SAME door a host reaches refuses it.

    This is the test that could not exist while the fixture built a bare
    `FastMCP`: the refusal lives in `CapabilityProfileMiddleware.on_call_tool`
    (`core/mcp_profiles.py:1094-1098` -> `enforce_result_model_channel` ->
    `model_channel.enforce_model_channel`), never in `compose_dossier` itself. It
    fails against a middleware-less target -- which is exactly the 2026-09-04
    deployment refusal (G15-T03) that 36 green tests failed to see.

    The deep link is left intact on purpose: a deep-link-only answer is the thing
    AC9 forbids, and it is what the tool would ship if this evidence were ever
    dropped in a refactor.
    """
    target, _ = wired
    monkeypatch.setattr(dossier_mcp, "_bounded_evidence", lambda **_kwargs: [])

    with pytest.raises(ToolError) as caught:
        await _compose(
            target,
            project_id=chain.project_id,
            label="August videos",
            blocks=[_figure(chain, spec_version_id)],
        )

    envelope = _envelope(caught.value)
    assert envelope["code"] == "evidence_required_with_deep_link"
    assert envelope["tool"] == "compose_dossier"


# ---------------------------------------------------------------------------
# The declaration (D2): exactly `save_notebook`'s, and the budget it costs.
# ---------------------------------------------------------------------------


def test_the_tool_is_declared_exactly_like_save_notebook():
    from core import mcp_profiles, notebook_mcp

    target = FastMCP("declarations")
    notebook_mcp.register(target)
    dossier_mcp.register(target)
    declarations = {d.name: d for d in mcp_profiles.registered_declarations()}
    ours, theirs = declarations["compose_dossier"], declarations["save_notebook"]
    assert (
        (ours.profile, ours.effect, ours.data_class, ours.confirmation_mode)
        == (theirs.profile, theirs.effect, theirs.data_class, theirs.confirmation_mode)
        == ("operations", "confirmed_write", "operational", "host")
    )
