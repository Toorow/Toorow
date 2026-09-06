"""The MCP tool surface, held against `docs/product-architecture/mcp-tool-surface.md`.

Two decisions, and each one has exactly one case here that fails when it is undone:

  * **AD-42** -- no MCP tool name carries a connector's name. Thirty-nine did, one
    per connector, because the core mounted each connector's `mcp_app` under its
    own namespace. They were 39 of the 92 tools a default host saw.
  * **AD-43** -- an undeclared tool does not reach discovery. Eighty-three did,
    because `_tool_profile` returned the always-visible `insights` for a tool with
    no `meta.profile` "so that discovery keeps listing it".

Both are asserted against the REAL assembled catalog (`core.main.mcp`), not a
hand-written list, because both defects are defects of accumulation: the next tool
or the next connector is the one that reintroduces them.

Offline. Importing `core.main` boots the app without touching Postgres, exactly as
the other catalog suites in this folder do.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def assembled():
    """The real app, its assembled tools and its loaded connectors."""
    from core import main as core_main  # noqa: PLC0415
    from core import mcp_profiles  # noqa: PLC0415

    return core_main, mcp_profiles


# ---------------------------------------------------------------------------
# AD-43 -- nothing reaches the catalog by omission.
# ---------------------------------------------------------------------------


def test_no_tool_reaches_the_catalog_without_a_declared_profile(assembled):
    """The gate this whole change exists to install.

    Without it the next tool added on plain `mcp.tool` inherits the permissive
    default again, and the surface grows back one omission at a time.
    """
    core_main, mcp_profiles = assembled

    undeclared = mcp_profiles.undeclared_assembled_tools(core_main.mcp)

    assert undeclared == [], (
        "these tools reach the assembled catalog with no capability declaration: "
        f"{undeclared}. Declare each with register_profiled(...) at its "
        "registration site -- see docs/product-architecture/mcp-tool-surface.md."
    )


def test_an_undeclared_tool_aborts_boot(assembled):
    """The declaration is enforced where it can be repaired: at boot, by name.

    A tool that merely vanished from discovery would be a silent removal of a
    capability a client may call today, which is the one outcome AD-43 refuses.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    _core_main, mcp_profiles = assembled

    undeclared = SimpleNamespace(name="a_tool_nobody_declared", meta=None, tags=set())
    fake_mcp = SimpleNamespace(
        providers=[SimpleNamespace(_components={"tool:a_tool_nobody_declared@1": undeclared})]
    )

    with pytest.raises(mcp_profiles.CatalogValidationError) as excinfo:
        mcp_profiles.assert_every_tool_is_declared(fake_mcp)

    message = str(excinfo.value)
    assert "a_tool_nobody_declared" in message, "the refusal must name the offender"
    assert "register_profiled" in message, "the refusal must name the repairing gesture"


def test_every_insights_tool_in_the_real_catalog_is_a_read(assembled):
    """The invariant that FORCED the write tools out of the default catalog.

    `_assert_consistent` has always refused an `insights` tool with a mutating
    effect. Nine tools that write reached the default Insights catalog anyway --
    not by contradicting this rule but by never meeting it, because they carried
    no declaration at all.
    """
    _core_main, mcp_profiles = assembled

    mutating_insights = [
        decl.name
        for decl in mcp_profiles.registered_declarations()
        if decl.profile == mcp_profiles.DEFAULT_PROFILE and decl.effect != "read"
    ]

    assert mutating_insights == []


# ---------------------------------------------------------------------------
# AD-42 -- no provider vocabulary in the tool namespace.
# ---------------------------------------------------------------------------


def test_no_tool_name_carries_a_connector_name(assembled):
    """Measured against the LOADED connectors, so a connector added next week counts.

    A hard-coded list of forbidden prefixes would pass forever the day someone
    drops a fortieth folder into `server/modules/`.
    """
    core_main, mcp_profiles = assembled

    connectors = sorted(loaded.name for loaded in core_main._loaded_modules)
    assert connectors, "no connector loaded -- this test would prove nothing"

    offenders = sorted(
        name
        for name in (
            getattr(tool, "name", "") or ""
            for tool in mcp_profiles.assembled_tools(core_main.mcp)
        )
        if any(
            name.startswith(f"{connector}_") or f"_{connector}_" in name
            for connector in connectors
        )
    )

    assert offenders == [], (
        f"these MCP tools carry a provider's name: {offenders}. A connector "
        "contributes extraction capabilities, mappings and Reports (AD-2) -- never "
        "an entry in the tool catalog (AD-42). What a project collects is exposed "
        "through its Datastreams: list_datastreams / get_datastream_report."
    )


def test_the_catalog_does_not_grow_with_the_connector_catalogue(assembled):
    """The property the thirty-nine tools broke, stated as a property.

    The connector catalogue is bounded by nothing, so any per-connector entry in
    the tool namespace makes the catalog's size a function of it.
    """
    core_main, mcp_profiles = assembled

    connectors = {loaded.name for loaded in core_main._loaded_modules}
    tool_names = {
        getattr(tool, "name", "") or ""
        for tool in mcp_profiles.assembled_tools(core_main.mcp)
    }

    # No tool name is derivable from a connector name: not as a namespace prefix,
    # and not as the connector's own identifier spelled with underscores.
    for connector in connectors:
        underscored = connector.replace("-", "_")
        assert not any(
            name.startswith(f"{connector}_") or name.startswith(f"{underscored}_")
            for name in tool_names
        ), f"a tool is named after the connector {connector!r}"


def test_the_replacement_pair_is_registered_and_agnostic(assembled):
    """AD-42 removed thirty-nine tools; these two are what answers instead.

    A test that only checked the removal would pass on a server that answers
    "what does this project collect?" with nothing at all.
    """
    _core_main, mcp_profiles = assembled

    declared = {decl.name: decl for decl in mcp_profiles.registered_declarations()}

    for name in ("list_datastreams", "get_datastream_report"):
        assert name in declared, f"{name} is not registered"
        assert declared[name].profile == "insights"
        assert declared[name].effect == "read"


# ---------------------------------------------------------------------------
# "Incomplete if" n. 8 -- a tool that mutates is reachable under `insights`.
#
# The clause was open for months while every test above was green, and the
# reason is worth stating: the tests above COUNT tools and check DECLARATIONS.
# Two tools declared `effect="read"` under `insights` and appended a row on the
# caller's word -- a rating in `app.feedback`, an inspection in
# `app.evidence_inspections` -- and no count could show it. What the declaration
# rests on is written in `mcp_profiles._record_app_declaration`: an append-only
# OBSERVATION transitions no DOMAIN state, so it is still a `read`. True, and
# incomplete: it is a read only while the append is impossible for a caller who
# merely knows the tool's name. The document carries the condition since
# 2026-08-30; these cases carry its proof.
#
# THE CENSUS IS IMPORTED, NOT COPIED. `scripts/mcp_tool_surface_report.py` prints
# the same split, from the same function. Two derivations of one rule is how the
# app-only question got three different answers before Story 67.7, and only one
# of the three actually removed anything.
#
# AND THE CENSUS WAS WRONG ONCE (2026-08-30, second pass). Importing it was right;
# trusting its first number was not. Its closure resolved `from core.x import fn`
# and dropped `from core import x` + `x.fn()`, so it reported NINE appenders where
# exact module resolution reports SIXTEEN -- and the tool it under-reported was
# `app_record_evidence_inspection`, the one the clause was opened for. The lesson
# is in `_bindings` of the script, and in
# `test_an_append_reached_through_a_module_binding_is_seen` below: an instrument
# whose blind spot is invisible reports a smaller world, and a smaller world is
# always the reassuring one.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))


#: An append governed by something OTHER than a handle, named by the SYMBOL that
#: governs it -- verified in the tool's own call closure, never taken on trust.
#: A prose exemption ages silently; a symbol that stops being called goes red.
_APPEND_GOVERNORS: dict[str, frozenset[str]] = {
    # `mcp-tool-surface.md` already states this one: "`submit_analyze_feedback`
    # requires a signed feedback context and refuses with `invalid_context`".
    "submit_analyze_feedback": frozenset({"verify_feedback_context"}),
}

#: APPENDS THAT ARE NOT THE CALLER'S WORD. One way: a name leaves this ledger
#: when it gains a handle, and adding one costs a sentence saying what the row
#: contains and why the caller could not have dictated it. That price is
#: deliberately higher than adding the handle.
#:
#: SEVEN NAMES WERE ADDED ON 2026-08-30 (second pass) and none of them is new
#: code. The census resolved `from core import mod` + `mod.fn()` to nothing, so
#: every append reached through a module binding was invisible; the first run
#: said nine appenders and the fixed resolver says sixteen. Each of the seven was
#: read at its write site before it was written down here -- an entry added to
#: make a number agree would be exactly the paper the clause forbids.
_SERVER_DERIVED_APPENDS: dict[str, str] = {
    "resolve_business_path": (
        "Appends `app.context_path_resolutions` through `_persist_resolved_path`: "
        "the path the SERVER walked through the MDM graph for the target the "
        "caller named. The caller dictates no field of the row."
    ),
    "search_context": (
        "Appends `app.context_candidate_fates` through `search_context_walk`: the "
        "fate the SERVER assigned to each candidate it ranked during this search. "
        "The caller supplies the query, never the row."
    ),
    "analyze_result": (
        "Appends `app.result_app_grants` through `_answer` -> "
        "`_compose_answer_on_connection` -> `result_app_grants.issue_handle`: the "
        "MINTING end of the very grant clause 18 requires elsewhere. The row is a "
        "handle over the Result the server just produced; the caller neither names "
        "it nor ever reads it back from a tool answer."
    ),
    "discover_analyze_matches": (
        "Appends `app.query_execution_attempts` through "
        "`_discover_analysis_context_options` -> `execute` -> "
        "`query_execution.accept_execution`: the ledger row of the probe the "
        "SERVER decided to run while matching. Identity, timing and outcome are "
        "the server's; the caller supplies the question."
    ),
    "execute_analyze_query_spec": (
        "Appends the whole execution ledger -- `app.query_execution_attempts` / "
        "`_events`, `app.query_results` / `app.query_result_payloads`, "
        "`app.datastream_output_used_by`, `app.ai_paths` / `app.ai_path_steps`, "
        "and the grant of `app.result_app_grants` -- for the query IT ran. Running "
        "a Query Spec IS producing those rows: the caller quotes a spec identity, "
        "and no field of the attempt, the payload or the recorded path is his."
    ),
    "explore_analyze_query": (
        "Appends `app.query_specs` / `app.query_spec_versions`: it MINTS the Query "
        "Spec identity that later reads quote. A producer of an identity is not an "
        "observation about one, and it cannot be asked for a grant that does not "
        "exist yet. It then runs that spec through `_execute_analyze_query_spec`, "
        "so the execution ledger above rides with it for the same reason."
    ),
    "render_analyze_result": (
        "Appends `app.feedback_eligible_observations`: it MINTS the eligibility -- "
        "and, through `result_app_grants.issue_handle`, the handle itself -- that "
        "the observation writers then consume. The minting end of the same grant. "
        "`app.query_execution_attempts` comes with the pivot sidecar it renders "
        "(`_pivot_render_sidecar` -> `execute`), a query the server chose to run. "
        "`app.visualizations` / `app.visualization_spec_versions` arrived with "
        "story 72.7's Chart Template door (`_materialize_pinned_spec` -> "
        "`template_materialization.materialize_template` -> "
        "`create_visualization_spec_version`), and they are the same case as "
        "`explore_analyze_query` minting a Query Spec version one entry above: the "
        "caller quotes two identities -- a template version and the Result -- and "
        "no FIELD of the row is his. The document is composed by the server from "
        "the template and the schema the Result declares, the identities are "
        "minted here, the ordinary validator judges it, and `proposed_by` records "
        "that a model asked. The compatibility verdict refuses before the first "
        "INSERT, so an incompatible template writes nothing at all."
    ),
    "get_card": (
        "Appends `app.render_snapshots` through `snapshots.persist_render_envelope`: "
        "a copy of the AD-1 envelope the SERVER just rendered, with a minted id, "
        "the caller's identity and the trace. Best-effort and never blocking -- the "
        "row records what was displayed, and the caller writes none of it."
    ),
    "datastream_diagnose": (
        "Appends `app.operations` / `app.operation_outbox` / `app.audit_log` "
        "through `_support_disclose` -> `operations.prepare_support_disclosure`, "
        "and ONLY on `disclose_support=True`. E36-NFR02 requires that audit to "
        "commit BEFORE the redacted evidence is returned, so the append is the "
        "guard rather than a side effect of one. Every field is server-computed -- "
        "`command_type=\"support.disclosure\"`, the resource path from the "
        "AD-5-guarded org, the idempotency key and both sha256 hashes from the "
        "diagnosis the server derived. The caller supplies a Datastream id and a "
        "boolean. THE WIDEST ENTRY IN THIS LEDGER: it is the only one whose rows "
        "live in the operations tables rather than an observation store, and it is "
        "named as such in the amendment so a reader can dispute it."
    ),
    "test_inbound_routing": (
        "Reached transitively only. `inbound_routing_test.run_datastream_routing_test` "
        "states its own contract in one line -- \"Walk the routing chain for one "
        "Datastream without writing anything\" -- and its body carries no INSERT. "
        "The one table, `app.datastream_inbound_credentials`, arrives four calls "
        "down through `inbound_credentials.resolve_by_token_hash` -> "
        "`_resolve_by_presented_hash` -> `_materialize_due_expirations`: the lazy "
        "expiry the credential lookup performs for itself, on every reader."
    ),
    "get_report": (
        "Appends four rows the SERVER derives while rendering: "
        "`app.render_snapshots` (the envelope it produced), "
        "`app.context_path_resolutions` (`resolve_report_paths` -> "
        "`_persist_resolved_path`, the same write already ledgered for "
        "`resolve_business_path`), `app.query_adherence` (the AD-18 verdict below) "
        "and `app.alert_firings` (`geographic_conformance."
        "record_unmapped_country_evidence` -> `emit_country_dq_firing`: the DQ "
        "evidence of an unmapped country the server MET). None is a field the "
        "caller can dictate."
    ),
    "get_daily_report": (
        "Appends `app.query_adherence` through `_apply_pre_query_gate` -> "
        "`adherence.record_data_query`: the verdict the SERVER computed about the "
        "caller's own tool sequence -- did a context call precede this data query "
        "in the same session. AD-18 says it is measured and never enforced, and the "
        "row is the measurement. The caller cannot write his own verdict; he can "
        "only be the subject of one."
    ),
    "get_datastream_report": (
        "Appends `app.query_adherence` through `_adherence.record_data_query` "
        "(2026-09-01, `analyze-and-test.md` amendment of that date): the same AD-18 "
        "verdict as `get_daily_report`, about the caller's own tool sequence, for "
        "the `fact_daily_kpi` roll-up this tool hands the model. Every field of the "
        "row -- session key, kind, verdict, context tool -- is the server's reading "
        "of what preceded the call; the caller supplies a Datastream and a window."
    ),
}


@pytest.fixture(scope="module")
def insights_appends(assembled):
    """The census, run once, against the real registry and the real `core/` source."""
    from mcp_tool_surface_report import insights_append_census  # noqa: PLC0415

    _core_main, mcp_profiles = assembled
    declared = {d.name: d for d in mcp_profiles.registered_declarations()}
    census = insights_append_census(declared)
    assert census, "the census found no appending insights tool -- it proves nothing"
    return census


def test_an_insights_tool_that_appends_consumes_a_server_minted_handle(insights_appends):
    """The clause, as a property of the whole profile rather than of two names.

    A tool that appends under `insights` must be stopped by something a caller
    cannot forge. The default is the grant the slice readers already require; the
    two alternatives are a named governor and a row the server derives, and both
    are ledgers above, not silence.
    """
    offenders = []
    for entry in insights_appends:
        tool = entry["tool"]
        if entry["consumes_a_handle"]:
            continue
        governors = _APPEND_GOVERNORS.get(tool)
        if governors is not None:
            missing = sorted(governors - set(entry["reaches"]))
            if missing:
                offenders.append(
                    f"{tool} ({entry['module']}) no longer calls its declared "
                    f"governor {missing}"
                )
            continue
        if tool in _SERVER_DERIVED_APPENDS:
            continue
        offenders.append(
            f"{tool} ({entry['module']}) appends {entry['appends_to']} with no handle"
        )

    assert offenders == [], (
        "these `insights` tools append and nothing a caller cannot forge stops "
        f"them: {offenders}. Give the tool the server-minted handle "
        "(`core.app_observation_handle`), or -- if the row is one the server "
        "derives rather than one the caller dictates -- name it in "
        "`_SERVER_DERIVED_APPENDS` with the sentence that says so. See "
        "docs/product-architecture/mcp-tool-surface.md, amendment of 2026-08-30."
    )


def test_the_two_observation_writers_take_and_consume_the_handle(insights_appends):
    """The repair itself, measured rather than assumed.

    `submit_feedback` and `app_record_evidence_inspection` are the two tools
    clause 8 was open for. Deriving this from the census means a signature that
    loses its `handle`, or a body that stops consuming it, fails HERE -- where a
    hand-written assertion about two names would only fail if someone remembered
    to update it.
    """
    by_tool = {entry["tool"]: entry for entry in insights_appends}

    for tool in ("submit_feedback", "app_record_evidence_inspection"):
        entry = by_tool.get(tool)
        assert entry is not None, f"{tool} no longer appears as an appending insights tool"
        assert entry["effect"] == "read", (
            f"{tool} declares effect={entry['effect']!r}; an observation is a read "
            "BEHIND A HANDLE, and any other effect leaves `insights` entirely"
        )
        assert entry["takes_a_handle"], f"{tool} lost its `handle` parameter"
        assert entry["consumes_a_handle"], (
            f"{tool} takes a handle and never consumes it -- a parameter nobody "
            "verifies is decoration, not a grant"
        )


def test_an_append_reached_through_a_module_binding_is_seen(insights_appends):
    """The resolver defect that made the first census say nine instead of sixteen.

    The closure collected `from core.x import name` bindings only, so
    `from core import mod` + `mod.fn()` -- the shape `core/` uses everywhere to
    break import cycles -- resolved to nothing and the edge was dropped. It is
    not a corner: `app_record_evidence_inspection` is the tool the whole clause
    was opened for, and the census credited it with the GRANT table alone, never
    reaching the `INSERT INTO app.evidence_inspections` the tool exists to make.
    Had the handle write not been there, the tool would have been missing from
    the census entirely -- and a census that cannot see its own subject cannot
    hold anything.

    Two write sites are pinned here, one per shape, because a resolver that
    regresses regresses silently: nothing goes red, the number just shrinks.
    """
    by_tool = {entry["tool"]: entry for entry in insights_appends}

    inspection = by_tool.get("app_record_evidence_inspection")
    assert inspection is not None, "the census lost the tool clause 18 was opened for"
    assert "app.evidence_inspections" in inspection["appends_to"], (
        "the census does not reach `evidence_inspections.insert_inspection`, "
        "called at `core/evidence_inspection_mcp.py` through the module bound by "
        f"`from core import evidence_inspections`. It sees only "
        f"{inspection['appends_to']} -- the resolver is dropping module bindings "
        "again."
    )

    daily = by_tool.get("get_daily_report")
    assert daily is not None, (
        "`get_daily_report` no longer appears as an appending insights tool -- it "
        "reaches `app.query_adherence` through `_adherence.record_data_query`, and "
        "a census that stops seeing it is a census that stopped resolving "
        "`from core import adherence as _adherence`"
    )
    assert "app.query_adherence" in daily["appends_to"], (
        f"`get_daily_report` appends {daily['appends_to']}; the adherence write of "
        "`core/reporting_mcp.py` (`_apply_pre_query_gate` -> "
        "`_adherence.record_data_query`) is not among them"
    )


def test_the_append_ledgers_cannot_outlive_their_tools(insights_appends):
    """A ledger that keeps a name nothing measures is a promise, not a record.

    Both ledgers are one-way, so the only way they stay honest is that a stale
    entry is loud: a renamed tool, a tool that stopped appending, or one that
    gained a handle must not keep a line here saying why it has none.
    """
    appending = {entry["tool"] for entry in insights_appends}
    handled = {entry["tool"] for entry in insights_appends if entry["consumes_a_handle"]}

    stale = sorted(
        name
        for name in set(_SERVER_DERIVED_APPENDS) | set(_APPEND_GOVERNORS)
        if name not in appending or name in handled
    )
    assert stale == [], (
        f"these names are excused from a rule they no longer meet: {stale}. Remove "
        "the entry -- an exemption for a tool that does not need one is how the "
        "next real offender inherits a green test."
    )


# ---------------------------------------------------------------------------
# "Incomplete if" n. 9 -- a declaration announces a `confirmation_mode` nobody plays.
#
# THE CEREMONY IS PLAYED BY THE RUNTIME, NOT BY THE BODIES, and the clause used to
# say the opposite ("la ceremonie AD-27 dans leur corps reste a ecrire"). What
# AD-27 asks of a `confirmed_write` is a trusted interactive surface; what proves
# it is a COLUMN of the capability-context row (migration 283), read at the call by
# `core.mcp_attestation` and refused centrally at `mcp_profiles.py#on_call_tool`
# for EVERY such declaration, before any body runs. The amendment of 2026-08-31
# defines the four modes once, and these cases are its measurement.
#
# TWO CASES, AND THE SECOND IS WHAT MAKES THE FIRST MEAN ANYTHING. A refusal test
# alone passes on a server that refuses everything -- the profile check would
# produce the same `not_found`. So the same grants, plus the presence column, must
# REACH the tool. The only difference between the two runs is that one field.
#
# AND A CENSUS, because the two-step token ceremony
# (`execute_confirmed_operation`) is owed only where a surface document demands a
# two-step act -- today exactly one does (`datastream-workbench-and-wizard.md`,
# "La porte MCP : cinq verbes, et la ceremonie reste entiere"). Pinning the set
# means the day a document demands a second one, an instrument already names who
# plays it and who does not.
# ---------------------------------------------------------------------------

#: Grants that make EVERY profile visible: authenticated, attested, endpoint and
#: workspace bound. Everything AD-27 asks except the presence itself, so a refusal
#: under them can only come from the presence gate.
_GRANTS_WITHOUT_PRESENCE = {
    "enabled_profiles": ["insights", "operations", "governance", "support"],
    "endpoint_binding": "endpoint-under-test",
    "workspace_evidence_hash": "b" * 64,
    "attested_context_id": "ctx-under-test",
}

#: The same row, with the column migration 283 added. 64 hex characters, which is
#: what `interactive_presence_verified` demands of a server-minted hash.
_GRANTS_WITH_PRESENCE = {
    **_GRANTS_WITHOUT_PRESENCE,
    "interactive_presence_evidence_hash": "c" * 64,
}

#: Tools that route through `execute_confirmed_operation` -- the whole two-step,
#: token included, played inside one call. A name is added here WITH the surface
#: document that demands it, never to make a number agree.
_TWO_STEP_CEREMONY_PLAYERS: dict[str, str] = {
    "materialize_datastream_draft": (
        "`datastream-workbench-and-wizard.md`, section `La porte MCP : cinq verbes, "
        "et la ceremonie reste entiere`: the wizard's publish-activate freezes a "
        "review, prepares a confirmation (reference + secret) and materializes by "
        "consuming it -- issue and consumption in the SAME transaction, so the "
        "secret never travels to the model."
    ),
}

#: EVERY tool that consumes a single-use confirmation, by any of the four seams
#: `_CONFIRMATION_CONSUMERS` names. Wider than the set above on purpose: pinning
#: only the wrapper would report a smaller, more reassuring world, and would have
#: said `39 tools consume nothing` while five do.
#:
#: A name here carries the document that DEMANDS its ceremony, or says plainly
#: that the code plays one no document demands -- which is not a defect, and is
#: exactly the kind of thing a census exists to keep readable.
_CONFIRMATION_CONSUMING_TOOLS: dict[str, str] = {
    **_TWO_STEP_CEREMONY_PLAYERS,
    "publish_semantic_model_change": (
        "`mcp-tool-surface.md`, amendment of 2026-08-25: `prepare_change_set` "
        "mints a single-use token and `confirm_change_set` consumes it, the whole "
        "sequence inside one server-side transaction so the token never enters "
        "model context. The document demands it in those words."
    ),
    "publish_country_master_data": (
        "Reaches `consume_entry_confirmation` through the country publication "
        "path. No surface document demands this two-step today: the code plays "
        "more than it is asked, which clause 9 does not hold against it."
    ),
    "edit_country_master_data": (
        "Same path as `publish_country_master_data`, same reading."
    ),
    "confirm_project_capability_change": (
        "Reaches `consume_entry_confirmation` and "
        "`consume_presence_bound_confirmation`: it IS the confirm half of a "
        "prepare/confirm pair, so its ceremony is played across two calls and "
        "consumed in this one. No surface document demands it in those words."
    ),
}


def _drive_call_tool(mcp_profiles, monkeypatch, tool_name, grants):
    """Call `on_call_tool` for *tool_name* under *grants*; return (refusal, reached)."""
    import asyncio  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    reached: list[str] = []

    async def call_next(_context):
        reached.append(tool_name)
        return "ok"

    monkeypatch.setattr(
        mcp_profiles,
        "_capability_context",
        lambda: ("person-under-test", {"host": "host-under-test"}, dict(grants)),
    )
    middleware = mcp_profiles.build_middleware()
    assert middleware is not None, "FastMCP middleware unavailable -- proves nothing"
    context = SimpleNamespace(message=SimpleNamespace(name=tool_name))
    try:
        asyncio.run(middleware.on_call_tool(context, call_next))
    except Exception as exc:  # noqa: BLE001 -- the refusal is the measurement.
        return str(exc), reached
    return None, reached


@pytest.fixture(scope="module")
def confirmed_writes(assembled):
    """Every registered `confirmed_write`, from the real registry."""
    _core_main, mcp_profiles = assembled

    declarations = [
        decl
        for decl in mcp_profiles.registered_declarations()
        if decl.effect == "confirmed_write"
    ]
    assert declarations, "no `confirmed_write` is registered -- this proves nothing"
    return declarations


def test_every_confirmed_write_is_refused_without_verified_presence(
    assembled, confirmed_writes, monkeypatch
):
    """Clause 9(a), over the WHOLE population rather than a sampled name.

    The caller here is as authorized as a caller gets short of AD-27: real
    identity, attested context, every profile enabled, endpoint and workspace
    bound. The one thing missing is the interactive-presence column, and it must
    be enough to close the door on all of them.
    """
    _core_main, mcp_profiles = assembled

    reachable = []
    for decl in confirmed_writes:
        refusal, reached = _drive_call_tool(
            mcp_profiles, monkeypatch, decl.name, _GRANTS_WITHOUT_PRESENCE
        )
        if reached or refusal is None or "Tool not found." not in refusal:
            reachable.append(f"{decl.name} ({decl.profile}/{decl.confirmation_mode})")

    assert reachable == [], (
        "these `confirmed_write` tools ran without verified interactive presence: "
        f"{reachable}. AD-27 is enforced in ONE place -- the "
        "`decl.effect == 'confirmed_write' and not interactive` branch of "
        "`mcp_profiles.build_middleware().on_call_tool` -- and the presence it "
        "reads is the capability-context column of migration 283, never a host's "
        "word. See docs/product-architecture/mcp-tool-surface.md, amendment of "
        "2026-08-31."
    )


def test_the_same_call_reaches_the_tool_once_presence_is_proven(
    assembled, confirmed_writes, monkeypatch
):
    """The control, without which the refusal above proves only that something refuses.

    Same tools, same grants, one field added. Every one must now reach `call_next`:
    a `confirmed_write` that stays hidden WITH presence is a tool no host can ever
    call, which is a different defect and just as invisible to a count.
    """
    _core_main, mcp_profiles = assembled

    refused = []
    for decl in confirmed_writes:
        refusal, reached = _drive_call_tool(
            mcp_profiles, monkeypatch, decl.name, _GRANTS_WITH_PRESENCE
        )
        if not reached:
            refused.append(f"{decl.name} ({decl.profile}) -> {refusal}")

    assert refused == [], (
        "these `confirmed_write` tools are refused even with verified interactive "
        f"presence and their profile enabled: {refused}. Either the profile is not "
        "one a capability context can enable, or the gate reads something the "
        "attestation does not carry."
    )


@pytest.fixture(scope="module")
def ceremony_census(assembled):
    """Who, among the `confirmed_write` tools, also plays the two-step."""
    from mcp_tool_surface_report import confirmed_write_ceremony_census  # noqa: PLC0415

    _core_main, mcp_profiles = assembled
    declared = {d.name: d for d in mcp_profiles.registered_declarations()}
    census = confirmed_write_ceremony_census(declared)
    assert census, "the census found no `confirmed_write` tool -- it proves nothing"
    return census


def test_the_census_sees_every_confirmed_write_declaration(
    confirmed_writes, ceremony_census
):
    """A census that cannot see its own subject cannot pin anything.

    Registered names and census names must be the same set. They diverge the day a
    `confirmed_write` is registered outside `server/core/*.py`, or the day the
    resolver stops recognising a registration form -- and both shrink the answer
    silently, which is the failure mode of every instrument on this page.
    """
    registered = {decl.name for decl in confirmed_writes}
    seen = {entry["tool"] for entry in ceremony_census}

    assert registered - seen == set(), (
        "these registered `confirmed_write` tools are invisible to the census: "
        f"{sorted(registered - seen)}. `_registered_tool_names` reads "
        "`server/core/*.py` only, and it reads three registration forms -- a tool "
        "registered elsewhere or in a fourth form needs the census extended, not "
        "this assertion relaxed."
    )
    assert seen - registered == set(), (
        f"the census names tools the registry does not: {sorted(seen - registered)}"
    )


def test_the_set_that_plays_the_two_step_is_pinned(ceremony_census):
    """Clause 9(b): the second ceremony is owed where a document demands it.

    Both directions are held. A name that ARRIVES means a body started consuming a
    per-call confirmation, and the document that demands it belongs in the ledger
    beside it. A name that LEAVES means a two-step a surface document still demands
    stopped being played -- which is exactly the shape clause 9 stays open for.
    """
    plays = sorted(e["tool"] for e in ceremony_census if e["plays_the_two_step"])

    assert plays == sorted(_TWO_STEP_CEREMONY_PLAYERS), (
        f"the tools routing through `execute_confirmed_operation` are {plays}; the "
        f"ledger records {sorted(_TWO_STEP_CEREMONY_PLAYERS)}. A new name is "
        "recorded WITH the surface document that demands its two-step; a lost name "
        "is a ceremony a document still demands and nobody plays -- clause 9 of "
        "docs/product-architecture/mcp-tool-surface.md."
    )


def test_every_tool_that_consumes_a_confirmation_is_pinned(ceremony_census):
    """The WIDER reading, so the pin above cannot understate what is played.

    `execute_confirmed_operation` is one seam of four. Measured over all of them,
    five `confirmed_write` tools consume a single-use confirmation -- and a census
    that named only the wrapper would have reported that thirty-nine consume
    nothing, which is the reassuring answer and the wrong one.
    """
    consuming = sorted(
        e["tool"] for e in ceremony_census if e["consumes_a_confirmation"]
    )

    assert consuming == sorted(_CONFIRMATION_CONSUMING_TOOLS), (
        f"the tools consuming a single-use confirmation are {consuming}; the "
        f"ledger records {sorted(_CONFIRMATION_CONSUMING_TOOLS)}. Record a new "
        "name with the seam it reaches and the document that demands it -- or with "
        "the sentence saying no document does, which is honest and cheap."
    )
