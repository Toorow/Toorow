"""toorow -- Source-agnostic MCP capability catalog service (Story 36.11, Epic 36).

E36-FR06 exposes four explicit MCP capability profiles from ONE service:
  * ``insights``    -- safe reads. The DEFAULT profile, always discoverable.
  * ``operations``  -- recoverable Datastream / pull management (opt-in).
  * ``governance``  -- approval / publication / reconciliation (opt-in).
  * ``support``     -- restricted human investigation (opt-in).

This module is the SINGLE source-agnostic catalog service. It never encodes a
Claude-first / ChatGPT-first host ordering (E36-NFR03) and it duplicates NO REST
business rule (E36-NFR05): it only *tags and filters* tools that other modules
already registered against their existing source-agnostic domain seams. There is
ZERO provider/source vocabulary here (E36-NFR05) -- profiles, effects and data
classes are the only taxonomy, and connection/host names arrive as opaque data.

Two independently versioned catalogs (E36-NFR04):
  * the "read" catalog  == insights-only tools;
  * the "admin" catalog == operations + governance + support tools.
``catalog_version("read")`` is a deterministic sha256 over the sorted read-tool
declarations, so an admin-catalog change NEVER perturbs the read-catalog version.
Hosts that cache/freeze tool definitions can rescan/republish one catalog without
destabilising ordinary report consumption.

FAIL CLOSED AT BOTH DISCOVERY AND CALL TIME:
  * ``validate_catalog()`` rejects at boot any tool that does not declare a valid
    profile/effect/data_class/confirmation_mode, or whose declaration is
    self-contradictory (an ``insights`` tool that writes, or a ``read`` tool that
    demands ``human`` confirmation).
  * ``CapabilityProfileMiddleware`` hides every non-insights tool from discovery
    unless the authenticated capability context opts into a higher profile bound to
    a verified endpoint/workspace, AND denies a direct call to any tool whose
    profile is not visible -- even when that tool was hidden from discovery (AC6).

THE GRANTS ARE ATTESTED, NOT CLAIMED (67-16). ``enabled_profiles``,
``endpoint_binding``, ``workspace_evidence_hash`` and the interactive-presence
evidence are read at EVERY call from the live ``app.mcp_capability_contexts``
row, by ``core.mcp_attestation`` -- the token carries only a pointer to it. A
revoked context therefore falls back to Insights at the NEXT call, not at token
expiry, exactly as ``session_revocation`` and ``render_shares.resolve_session``
revalidate their own living state. Absent a row, high-risk profiles simply stay
unavailable (fail closed): no host self-report can raise a profile above
``insights``, and the deployment-wide ``TOOROW_MCP_HIGHRISK_ENABLED`` escape
hatch that stood in for this verification is retired with it.

Mirrors the metric_semantics_mcp conventions: ``from __future__ import
annotations``, module logger, lazy ``fastmcp`` imports inside function bodies (no
import cycle with core.main), English user-facing error microcopy, ASCII-only.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Taxonomy -- the ONLY vocabulary this module owns. No source/provider names.
# ---------------------------------------------------------------------------
PROFILES: tuple[str, ...] = ("insights", "operations", "governance", "support")

# ``insights`` is the sole default. Every other profile is policy opt-in and
# endpoint/workspace bound (E36-NFR03: capability-negotiated, no host ordering).
DEFAULT_PROFILE = "insights"

# AD-24 names exactly three effects: a read cannot mutate, a prepare cannot
# authorize, and a confirmed write must go through the AD-27 ceremony. The generic
# ``write`` this catalog used until Story 48.1 could not express that difference --
# it labelled a non-authorizing proposal and a consequential mutation identically.
EFFECTS: tuple[str, ...] = ("read", "prepare", "confirmed_write")

# Coarse data sensitivity classes attached to each tool for governance/audit.
DATA_CLASSES: tuple[str, ...] = ("public", "operational", "sensitive")

# Reuse operations.py's confirmation vocabulary verbatim (do not fork it).
CONFIRMATION_MODES: tuple[str, ...] = ("none", "server", "host", "human")

# The two independently versioned connection classes (E36-NFR04).
CONNECTION_CLASSES: tuple[str, ...] = ("read", "admin")

# Profiles considered high-risk: they stay unavailable until the capability
# context carries a server-verifiable workspace evidence hash (fail closed).
_HIGH_RISK_PROFILES: frozenset[str] = frozenset({"operations", "governance", "support"})


class CatalogValidationError(RuntimeError):
    """Raised at import/boot when a tool declaration is missing or contradictory."""


# Story 50.6 -- the data/render tool split, made a closed contract instead of a rule.
#
# `visualization-and-rendering.md` ("Tool split"): a data tool does NOT attach a
# widget resource; only the render tool advertises one. A story that edits four
# call sites leaves the fifth to be written next week, so the invariant lives here
# and `validate_catalog()` aborts boot on a violation -- exactly as an `insights`
# tool declaring a mutating effect already does.
#
# The name below is the ONE tool permitted to carry a widget-resource binding.
# It is a name, not a list: a second render tool is a design decision, and it must
# be taken here rather than acquired by accident.
RENDER_TOOL_NAME = "render_analyze_result"

#: Tools that declared `visibility=["app"]` through `register_profiled`. An app-only
#: tool is reachable by a mounted widget, never offered to a model as an analytical
#: capability (`skill_tool_catalog` reads this).
_APP_ONLY_TOOLS: set[str] = set()

#: The two app-only tools whose declarations a host must discover in
#: ``tools/list`` to route a widget call. The other app-only tools remain
#: registered and callable by their stable names, but retain the pre-67.7
#: discovery posture: absent from the model-facing wire catalog.
HOST_ROUTED_APP_ONLY_TOOLS = frozenset({"app_read_result_slice", "submit_feedback"})

#: Tools that declared a widget resource at registration, with the URI they bound.
_WIDGET_BOUND_TOOLS: dict[str, str] = {}


def app_only_tool_names() -> frozenset[str]:
    """Names declared `visibility=["app"]` -- read by the Skill-authoring projection."""
    return frozenset(_APP_ONLY_TOOLS)


def declared_ui_visibility(tool: Any) -> list[str] | None:
    """Read the MCP Apps visibility a tool declared, from its wire ``meta.ui``.

    FastMCP 3.4.4 projects ``AppConfig(visibility=["app"])`` onto
    ``tool.meta["ui"]["visibility"]``, which is the field a host reads in
    ``tools/list``. Reading THAT rather than a parallel registry is the point: a
    second source of truth is a second thing to keep in step.
    """
    meta = getattr(tool, "meta", None)
    if isinstance(meta, dict):
        ui = meta.get("ui")
        if isinstance(ui, dict):
            visibility = ui.get("visibility")
            if isinstance(visibility, (list, tuple)):
                return [str(v) for v in visibility]
    return None


def is_app_only_tool(tool: Any) -> bool:
    """True for a tool that exists for a mounted widget, not for a model.

    THE ONE PREDICATE. Three readers ask this question -- the discovery filter in
    `CapabilityProfileMiddleware.on_list_tools`, the Skill-authoring projection in
    `core.skill_tool_catalog`, and the offline measurement in
    `scripts/mcp_tool_surface_report.py`. They asked it three ways until 67.7, and
    only one of the three actually removed anything.

    The wire declaration decides; the registration-time registry is the fallback
    for a tool whose `meta` never made the trip (a transformed or proxied copy).
    """
    visibility = declared_ui_visibility(tool)
    if visibility is not None:
        return visibility == ["app"]
    name = getattr(tool, "name", None)
    return isinstance(name, str) and name in _APP_ONLY_TOOLS


def model_visible_tools(tools: Any, *, allowed: frozenset[str], interactive: bool) -> list[Any]:
    """Filter an assembled catalog down to what the MCP wire may advertise.

    Two exclusions, one place (`mcp-tool-surface.md`, "Ce que le catalogue coute"):

    1. a tool whose declared profile is not in the caller's visible set -- an
       undeclared tool has no profile and so is in nobody's set (AD-43);
    2. a `confirmed_write` when interactive presence is not proven -- a host that
       cannot complete the AD-27 ceremony should not be shown the action.

    App-only declarations are excluded by default. Two explicitly reviewed host-
    routed tools are allowed through because their hosts discover and route them
    from ``tools/list``. Every app-only tool remains registered and callable by its
    stable name; generated model Skills exclude the whole app-only set through
    :func:`is_app_only_tool`.
    """
    return [
        tool
        for tool in tools
        if _tool_profile(tool) in allowed
        and (interactive or _tool_effect(tool) != "confirmed_write")
        and (
            not is_app_only_tool(tool) or getattr(tool, "name", None) in HOST_ROUTED_APP_ONLY_TOOLS
        )
    ]


def widget_bound_tools() -> dict[str, str]:
    """Snapshot of `{tool_name: resource_uri}` for every declaration-level binding."""
    return dict(_WIDGET_BOUND_TOOLS)


@dataclass(frozen=True)
class ToolDeclaration:
    """One immutable capability declaration recorded at registration time."""

    name: str
    profile: str
    effect: str
    data_class: str
    confirmation_mode: str

    def connection_class(self) -> str:
        """ "read" for insights tools, "admin" for every opt-in profile."""
        return "read" if self.profile == DEFAULT_PROFILE else "admin"


@dataclass
class _Registry:
    """In-process registry of every profiled tool declaration (name-keyed)."""

    declarations: dict[str, ToolDeclaration] = field(default_factory=dict)

    def record(self, decl: ToolDeclaration) -> None:
        # A duplicate name with a DIFFERENT declaration is a contradiction: fail
        # closed rather than let a later registration silently widen a profile.
        existing = self.declarations.get(decl.name)
        if existing is not None and existing != decl:
            raise CatalogValidationError(
                f"tool {decl.name!r} re-declared with a different capability profile"
            )
        self.declarations[decl.name] = decl

    def reset(self) -> None:
        self.declarations.clear()


# Module-level registry. Populated as modules call ``register_profiled`` from their
# own ``register(mcp)`` hooks; validated once by ``validate_catalog()`` at boot.
_REGISTRY = _Registry()


def reset_registry_for_tests() -> None:
    """Clear the in-process registry so a test starts from a known-empty state."""
    _REGISTRY.reset()
    _APP_ONLY_TOOLS.clear()
    _WIDGET_BOUND_TOOLS.clear()


def registered_declarations() -> tuple[ToolDeclaration, ...]:
    """Return every recorded declaration (read-only snapshot, deterministic order)."""
    return tuple(sorted(_REGISTRY.declarations.values(), key=lambda d: d.name))


# ---------------------------------------------------------------------------
# Registration -- tag + meta a tool AND record its declaration in one call.
# ---------------------------------------------------------------------------


def register_profiled(
    mcp,
    handler: Callable[..., Any],
    *,
    profile: str,
    effect: str,
    data_class: str,
    confirmation_mode: str,
    name: str | None = None,
    app=None,
):
    """Register *handler* on *mcp* with its capability profile, and record it.

    Attaches FastMCP component ``tags`` (``profile:*``/``effect:*``/``data_class:*``)
    and ``meta`` (the four attributes) so ``CapabilityProfileMiddleware`` can filter
    discovery and calls off the registered tool's metadata (E36-FR06). The same
    declaration is recorded in the in-process registry for ``validate_catalog`` and
    ``catalog_version``.

    Verified against FastMCP 3.4.4: ``FastMCP.tool`` accepts ``tags: set[str]`` and
    ``meta: dict[str, Any]``; the returned Tool exposes ``.name``/``.tags``/``.meta``.
    Returns *handler* unchanged so registration can be chained/decorated.

    Story 50.6 adds ``app`` -- a ``fastmcp.apps.AppConfig`` forwarded verbatim to
    ``mcp.tool``. Until this parameter existed, a profiled tool could not be
    ``visibility=["app"]``: ``submit_feedback`` is app-only precisely because it is
    registered on plain ``mcp.tool``, OUTSIDE the capability middleware. Without
    this, an app-only slice reader would have had to be registered outside the only
    mechanism that filters discovery by profile -- putting the least
    model-appropriate tool in the least governed place, which is the wrong
    direction. Both declarations (app-only visibility, widget-resource binding) are
    recorded so ``validate_catalog`` can enforce the data/render split across the
    whole catalog rather than at four call sites.
    """
    _reject_unknown(profile, effect, data_class, confirmation_mode)
    tool_name = name or handler.__name__
    decl = ToolDeclaration(
        name=tool_name,
        profile=profile,
        effect=effect,
        data_class=data_class,
        confirmation_mode=confirmation_mode,
    )
    _assert_consistent(decl)  # fail closed at registration, not only at boot
    _record_app_declaration(tool_name, decl, app)
    _REGISTRY.record(decl)
    kwargs = {
        "name": name,
        "tags": {
            f"profile:{profile}",
            f"effect:{effect}",
            f"data_class:{data_class}",
        },
        "meta": {
            "profile": profile,
            "effect": effect,
            "data_class": data_class,
            "confirmation_mode": confirmation_mode,
        },
    }
    if app is not None:
        kwargs["app"] = app
    mcp.tool(handler, **kwargs)
    return handler


def _record_app_declaration(tool_name: str, decl: ToolDeclaration, app) -> None:
    """Record and validate an ``AppConfig`` at registration -- fail closed, at the source.

    Two rules, both enforced before the tool is on the server rather than at boot,
    so the traceback names the registration site:

      * an app-only tool must be ``effect="read"`` with ``confirmation_mode="none"``.
        A tool the model cannot see and cannot be shown in a catalog, that mutates,
        is a side channel with a confirmation ceremony nobody can perform.
      * only ``RENDER_TOOL_NAME`` may bind a widget resource. That is the data/render
        split, and it is the whole point of Story 50.6.

    WHAT "MUTATES" MEANS HERE -- settled, so it is not re-litigated a third time.

    ``effect`` classifies what a tool does to DOMAIN state: AD-24 says "a read
    cannot mutate, a prepare cannot authorize, and a confirmed write must use
    AD-27". It does not classify the audit a call leaves behind, and it cannot:
    AD-28 requires "every read of sensitive samples, account exposure, ..." to
    commit "the state transition, append-only audit event and outbox record
    together". A vocabulary in which writing an append-only audit row promoted a
    tool out of ``read`` would make AD-28 unimplementable for every read tool in
    the catalog.

    So an app-only tool that records an OBSERVATION -- append-only, no domain
    transition, reachable by the audited RGPD erasure, "evidence that a surface
    was USED, never a second copy of what it displayed" (migration 175) -- is
    ``effect="read"``, and that is not a false declaration. Migration 175 recorded
    the opposite conclusion in a comment ("cannot be declared under the three
    effects of AD-24 without lying about one of them") and named a fourth effect
    or an app-only exemption as the way out. Neither is needed: the exemption is
    the rule above, it has existed since Story 50.6, and ``app_read_result_manifest``
    / ``app_read_result_slice`` already ship under it. Migration 177 replaces that
    comment rather than leaving it to be believed by the next reader.

    CE TROU EST FERME depuis AD-43, et le paragraphe qui le decrivait est retire
    plutot que laisse a croire (2026-08-22, story 67.7). Il disait :
    ``submit_feedback`` est enregistre sur ``mcp.tool`` nu, donc sans declaration.
    Mesure : ``feedback_mcp.register`` passe par ``register_profiled``
    (`core/feedback_mcp.py#register`), et ``validate_catalog`` refuse au boot tout outil du
    catalogue ASSEMBLE qui ne porte pas de declaration. Un commentaire qui decrit
    un trou refermé envoie chercher une reparation deja faite.
    """
    if app is None:
        return
    visibility = getattr(app, "visibility", None)
    resource_uri = getattr(app, "resource_uri", None)
    if visibility and list(visibility) == ["app"]:
        if decl.effect != "read" or decl.confirmation_mode != "none":
            raise CatalogValidationError(
                f"app-only tool {tool_name!r} must be effect=read/confirmation_mode=none; "
                f"a hidden mutating tool is a side channel"
            )
        _APP_ONLY_TOOLS.add(tool_name)
    if resource_uri:
        if tool_name != RENDER_TOOL_NAME:
            raise CatalogValidationError(
                f"tool {tool_name!r} declares widget resource {resource_uri!r}; only "
                f"{RENDER_TOOL_NAME!r} may advertise a widget (data/render tool split)"
            )
        _WIDGET_BOUND_TOOLS[tool_name] = str(resource_uri)


def _reject_unknown(profile: str, effect: str, data_class: str, confirmation_mode: str) -> None:
    if profile not in PROFILES:
        raise CatalogValidationError(f"unknown profile: {profile!r}")
    if effect not in EFFECTS:
        raise CatalogValidationError(f"unknown effect: {effect!r}")
    if data_class not in DATA_CLASSES:
        raise CatalogValidationError(f"unknown data_class: {data_class!r}")
    if confirmation_mode not in CONFIRMATION_MODES:
        raise CatalogValidationError(f"unknown confirmation_mode: {confirmation_mode!r}")


def _assert_consistent(decl: ToolDeclaration) -> None:
    """Raise when a single declaration is internally contradictory (fail closed).

    Four invariants, one per sentence of the AD-24 rule:
      * an ``insights`` tool must be ``effect=read`` (Insights is safe reads only);
      * a ``read`` tool never demands confirmation -- it transitions no domain
        state, so there is nothing for a human to authorize. It may still leave
        the append-only audit AD-28 requires of every read; see
        ``_record_app_declaration`` for why that is not a mutation;
      * a ``prepare`` never demands host/human confirmation. A prepare that took a
        human approval would be authorizing, which is precisely what it must not
        be: ``none`` or server-side verification only;
      * a ``confirmed_write`` always demands ``host`` or ``human``. A consequential
        mutation with ``none`` would bypass the AD-27 ceremony, and the point of
        splitting the old generic ``write`` was to make that undeclarable.
    """
    if decl.profile == DEFAULT_PROFILE and decl.effect != "read":
        raise CatalogValidationError(
            f"insights tool {decl.name!r} must be effect=read (safe reads only)"
        )
    if decl.effect == "read" and decl.confirmation_mode != "none":
        raise CatalogValidationError(
            f"read tool {decl.name!r} cannot require confirmation_mode={decl.confirmation_mode!r}"
        )
    if decl.effect == "prepare" and decl.confirmation_mode not in {"none", "server"}:
        raise CatalogValidationError(
            f"prepare tool {decl.name!r} cannot authorize with confirmation_mode="
            f"{decl.confirmation_mode!r}"
        )
    if decl.effect == "confirmed_write" and decl.confirmation_mode not in {"host", "human"}:
        raise CatalogValidationError(
            f"confirmed write {decl.name!r} requires a trusted confirmation, not "
            f"confirmation_mode={decl.confirmation_mode!r}"
        )


# ---------------------------------------------------------------------------
# Startup validation -- fail closed at boot (AC1).
# ---------------------------------------------------------------------------


def assembled_tools(mcp) -> list[Any]:
    """Enumerate every tool on *mcp* SYNCHRONOUSLY, or fail closed.

    FastMCP 3.4.4 exposes tool listing as a coroutine, and the boot validator runs
    at module import where starting an event loop is not always safe. The
    provider's ``_components`` mapping is the same registry that coroutine reads,
    keyed ``tool:<name>@<version>``, so it is used first and the coroutine is only
    the fallback. If NEITHER works this raises: a catalog invariant that quietly
    skips itself when it cannot read the catalog is not an invariant.
    """
    tools: list[Any] = []
    providers = getattr(mcp, "providers", None) or []
    for provider in providers:
        components = getattr(provider, "_components", None)
        if not isinstance(components, dict):
            continue
        for key, component in components.items():
            if isinstance(key, str) and key.startswith("tool:"):
                tools.append(component)
    if tools:
        return tools
    try:
        import asyncio  # noqa: PLC0415

        return list(asyncio.run(mcp._list_tools()))
    except Exception as exc:  # noqa: BLE001
        raise CatalogValidationError(
            "the assembled tool catalog could not be enumerated, so the data/render "
            "split could not be verified"
        ) from exc


def _tool_widget_resource(tool: Any) -> str | None:
    """Return the widget resource a tool advertises through ``meta.ui.resourceUri``."""
    meta = getattr(tool, "meta", None)
    if isinstance(meta, dict):
        ui = meta.get("ui")
        if isinstance(ui, dict):
            uri = ui.get("resourceUri")
            if isinstance(uri, str) and uri.strip():
                return uri.strip()
    return None


def assert_data_render_split(mcp) -> dict[str, str]:
    """AC12 -- at most one tool in the whole catalog advertises a widget resource.

    Driven off the LIVE registry, never a hand-written list of tool names, so a
    tool added next week is inside the invariant by construction rather than by
    someone remembering to add it. Returns the surviving binding
    ``{tool_name: resource_uri}`` -- empty when the render tool is not registered,
    which is a legitimate state and must stay visible rather than be papered over.
    """
    bound = {
        name: uri
        for name, uri in (
            (getattr(tool, "name", None), _tool_widget_resource(tool))
            for tool in assembled_tools(mcp)
        )
        if uri and isinstance(name, str)
    }
    offenders = sorted(name for name in bound if name != RENDER_TOOL_NAME)
    if offenders:
        raise CatalogValidationError(
            "data/render tool split violated: "
            + ", ".join(f"{name!r} advertises {bound[name]!r}" for name in offenders)
            + f"; only {RENDER_TOOL_NAME!r} may advertise a widget resource"
        )
    return bound


# ---------------------------------------------------------------------------
# Story 50.6 -- the model-channel budget, enforced ONCE for the whole catalog.
# ---------------------------------------------------------------------------
#
# AD-1 and AC4 say the budget is "enforced by ONE shared server-side guard rather
# than by each tool's discipline". It was each tool's discipline, at four remembered
# call sites in `core/main.py`, and the catalog measured 4 guarded against 15
# unguarded functions building `structured_content=` in that file alone, plus eight
# further modules that build a `ToolResult` and never import `core.model_channel`.
# `list_card_templates` shipped a 7255-byte `structuredContent` against a 4096-byte
# budget, in the assembled catalog, guarded by nothing.
#
# Four sites is a habit. A habit does not cover the sixteenth tool, and the test
# that "proved" the budget only asserted that two strings appeared somewhere in two
# files -- it passed unchanged when an unguarded tool shipped.
#
# So the budget moves to where the data/render split already is: a hook on every
# returning tool result. A tool cannot opt out of it, cannot forget it, and cannot
# be written next week without it.


def enforce_result_model_channel(tool_name: str, result: Any) -> Any:
    """Split then refuse, on the result of ANY tool call. Returns the bounded result.

    Two operations, in the order that matters:

      1. `partition_envelope` routes dataset-shaped values into the app channel
         (`_meta`), leaving a STATED descriptor -- the exact row count and the
         column names -- where the data was. Nothing is discarded and nothing is
         silently trimmed, so a model-visible payload can never read as a complete
         dataset that is actually a truncated one.
      2. `enforce_model_channel` REFUSES what is still over budget, naming the
         tool, the measured size and the budget. A tool that legitimately needs
         more must say so through the bounded path; it does not get to escape.

    A result whose `structured_content` is not a dict (a scalar, a list, nothing at
    all) is still measured -- the text-line budget and the byte budget both apply --
    it simply has no structure to split.

    The refusal leaves as a `ToolError` carrying the canonical `{code, message,...}`
    JSON, the same shape the capability denial above uses, so a host reads one error
    vocabulary from this middleware rather than an internal server error.
    """
    from core import model_channel  # noqa: PLC0415

    content = getattr(result, "content", None)
    structured = getattr(result, "structured_content", None)
    app_payload: dict[str, Any] = {}
    model_visible = structured
    if isinstance(structured, dict):
        model_visible, app_payload = model_channel.partition_envelope(
            structured, tool_name=tool_name
        )
    try:
        model_channel.enforce_model_channel(tool_name, content, model_visible)
    except (
        model_channel.ModelChannelOverBudget,
        model_channel.ModelChannelEvidenceMissing,
    ) as exc:
        raise _tool_error_payload(exc.as_dict()) from exc
    if not app_payload:
        # Nothing was routed, so nothing changed: every rewrite the split performs
        # also records what it moved. Returning the ORIGINAL result -- not an equal
        # copy -- keeps a tool's own `_meta` (the Analyze adapter's `toorow.result`)
        # exactly as the tool built it.
        return result
    meta = dict(getattr(result, "meta", None) or {})
    existing = meta.get(model_channel.APP_PAYLOAD_META_KEY)
    if (
        isinstance(existing, dict)
        and existing.get("schema_version") == 1
        and existing.get("kind") == "render"
    ):
        typed = dict(existing)
        moved = dict(existing.get("moved") or {})
        moved.update(app_payload)
        typed["moved"] = moved
        meta[model_channel.APP_PAYLOAD_META_KEY] = typed
        from core import render_app_payload  # noqa: PLC0415

        try:
            render_app_payload.validate_render_app_payload(typed, existing_meta=meta)
        except render_app_payload.RenderAppPayloadRefused as exc:
            raise _tool_error_payload(exc.as_dict()) from exc
    else:
        wrapped = model_channel.app_meta(app_payload)
        if wrapped:
            meta.update(wrapped)
    # `model_copy` rather than a fresh `ToolResult(...)`: the content blocks have
    # already been normalized by the tool layer and must not be re-converted.
    return result.model_copy(update={"structured_content": model_visible, "meta": meta or None})


def undeclared_assembled_tools(mcp) -> list[str]:
    """Names present in the ASSEMBLED catalog that carry no capability declaration.

    Read off the same enumeration ``assert_data_render_split`` uses, so "what the
    server actually holds" has one definition here rather than two that drift.
    """
    declared = set(_REGISTRY.declarations)
    return sorted(
        name
        for name in (getattr(tool, "name", None) for tool in assembled_tools(mcp))
        if isinstance(name, str) and name and name not in declared
    )


def assert_every_tool_is_declared(mcp) -> None:
    """AD-43 -- an undeclared tool aborts BOOT; it does not quietly become Insights.

    The runtime backstop lives in ``_tool_profile`` (undeclared -> visible to
    nobody), but a backstop that silently removes a tool a client calls today is
    the wrong place for this to be caught. It is caught here, at boot, naming every
    offender, so the answer is a declaration rather than a disappearance.

    Driven off the LIVE assembled catalog rather than a hand-written list, so the
    connector or module added next week is inside the invariant by construction.
    """
    offenders = undeclared_assembled_tools(mcp)
    if offenders:
        raise CatalogValidationError(
            "these tools reach the assembled catalog with no capability declaration: "
            + ", ".join(repr(name) for name in offenders)
            + "; declare each with register_profiled(profile=..., effect=..., "
            "data_class=..., confirmation_mode=...) -- see "
            "docs/product-architecture/mcp-tool-surface.md (AD-43)"
        )


def validate_catalog(mcp=None) -> tuple[ToolDeclaration, ...]:
    """Validate every registered tool at boot; raise on any gap/contradiction.

    Guarantees, before the server accepts a single call:
      * every tool declares a VALID profile/effect/data_class/confirmation_mode;
      * no declaration is self-contradictory (``_assert_consistent``);
      * every ``insights`` tool is ``effect=read`` (redundant safety net for AC1);
      * (Story 50.6) at most one tool in the ASSEMBLED catalog advertises a widget
        resource, and it is the declared render tool;
      * (AD-43) every tool in the ASSEMBLED catalog carries a declaration at all.
        Passing *mcp* enables the last two checks; omitting it validates the
        profiled declarations only, which is what the many existing unit tests of
        this module do.

    Called once from core.main after all ``register(mcp)`` hooks have run. Returns
    the validated declarations for a snapshot test. Raises ``CatalogValidationError``
    (fail closed) so a mis-declared tool aborts boot rather than shipping unguarded.
    """
    declarations = registered_declarations()
    for decl in declarations:
        _reject_unknown(decl.profile, decl.effect, decl.data_class, decl.confirmation_mode)
        _assert_consistent(decl)
    # Explicit AC1 assertion: Insights is read-only across the whole catalog.
    for decl in declarations:
        if decl.profile == DEFAULT_PROFILE and decl.effect != "read":
            raise CatalogValidationError(f"insights tool {decl.name!r} declares a mutating effect")
    if mcp is not None:
        assert_data_render_split(mcp)
        assert_every_tool_is_declared(mcp)
    logger.info(
        "mcp_profiles: validated %d tool(s) across %d profile(s)",
        len(declarations),
        len({d.profile for d in declarations}),
    )
    return declarations


# ---------------------------------------------------------------------------
# Independently versioned catalogs (E36-NFR04).
# ---------------------------------------------------------------------------


def catalog_version(connection_class: str) -> str:
    """Return a deterministic sha256 catalog version for *connection_class*.

    ``"read"`` covers insights-only tools; ``"admin"`` covers operations/governance/
    support. The hash is taken over the SORTED ``(name, profile, effect, data_class,
    confirmation_mode)`` tuples of that class only, so the two catalogs are versioned
    independently: adding/removing an admin tool never changes the read version, and
    Insights behaviour stays stable across admin-catalog churn (E36-NFR04). Pure and
    deterministic -- no time, no randomness -- so hosts can compare cached versions.
    """
    if connection_class not in CONNECTION_CLASSES:
        raise ValueError(f"unknown connection_class: {connection_class!r}")
    members = [
        (d.name, d.profile, d.effect, d.data_class, d.confirmation_mode)
        for d in registered_declarations()
        if d.connection_class() == connection_class
    ]
    members.sort()
    canonical = json.dumps(members, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Visibility resolution -- which profiles a caller may see/use (E36-FR06).
# ---------------------------------------------------------------------------


def visible_profiles(
    identity: str | None,
    host_context: dict[str, Any] | None,
    grants: dict[str, Any] | None,
) -> frozenset[str]:
    """Return the set of profiles visible to *identity* in *host_context*.

    Insights is ALWAYS visible (the safe-read default). A high-risk profile
    (operations/governance/support) is visible only when ALL hold:
      * the caller is authenticated (a real identity, not ``anonymous``/empty);
      * the grants were ATTESTED -- read by ``core.mcp_attestation`` from a live,
        unrevoked ``app.mcp_capability_contexts`` row, which is the only thing
        that ever sets ``attested_context_id``;
      * that row explicitly enables the profile via ``enabled_profiles`` (policy
        opt-in -- this module never re-derives org/project rights);
      * it is bound to a verified endpoint/workspace: a non-empty
        ``endpoint_binding`` AND a 64-hex ``workspace_evidence_hash``.

    Absent proof, high-risk profiles fail closed regardless of any host self-report
    (AC4). No host is preferred over another (E36-NFR03): the decision is purely
    capability/evidence driven, host name is opaque data.
    """
    visible = {DEFAULT_PROFILE}
    if not identity or identity == "anonymous":
        return frozenset(visible)
    grants = grants or {}
    enabled = grants.get("enabled_profiles") or [DEFAULT_PROFILE]
    if not isinstance(enabled, (list, tuple, set)):
        return frozenset(visible)
    if not _endpoint_workspace_verified(grants):
        return frozenset(visible)  # no proof -> insights only
    # THE TRUST BOUNDARY (review C1, decided 2026-08-17). The three fields checked
    # just above used to arrive from the caller's token claims and were only
    # shape-checked, so a well-formed self-report was indistinguishable from a real
    # binding; the compensation was a deployment-wide TOOROW_MCP_HIGHRISK_ENABLED
    # flag that was either shut (governed writes unreachable everywhere) or open
    # (every claim believed). Both halves are gone: `_capability_context` now reads
    # the grants from the live capability-context row and stamps
    # `attested_context_id`, which nothing else can produce. A revoked row stops
    # attesting, so the cut is felt at the NEXT call.
    if not context_attested(grants):
        return frozenset(visible)  # unattested claim -> insights only
    for profile in enabled:
        if profile in _HIGH_RISK_PROFILES:
            visible.add(profile)
    return frozenset(visible)


def interactive_presence_verified(grants: dict[str, Any] | None) -> bool:
    """True only when the capability context carries server-minted presence evidence.

    A ``confirmed_write`` needs more than an authorized profile: AD-27 requires a
    trusted interactive surface, and a non-interactive host may prepare and inspect
    but never authorize. Presence is proven by a 64-hex evidence hash the server
    minted for this endpoint/workspace -- never by a host claiming to be interactive,
    which is why a missing hash hides the tool rather than merely refusing it.

    Since 67-16 that hash is a COLUMN of the capability context (migration 283)
    and reaches this function only through attestation, so an unattested caller
    can no longer assert its own presence.
    """
    grants = grants or {}
    if not _endpoint_workspace_verified(grants):
        return False
    if not context_attested(grants):
        return False
    evidence = grants.get("interactive_presence_evidence_hash")
    if not isinstance(evidence, str) or len(evidence) != 64:
        return False
    return all(c in "0123456789abcdef" for c in evidence)


def context_attested(grants: dict[str, Any] | None) -> bool:
    """True only when these grants were read from a live capability-context row.

    ``attested_context_id`` is written by ``_capability_context`` and by nothing
    else; a token claim of that name never reaches here, because the grants dict
    is rebuilt from the row rather than copied from the claims.
    """
    context_id = (grants or {}).get("attested_context_id")
    return isinstance(context_id, str) and bool(context_id.strip())


def _endpoint_workspace_verified(grants: dict[str, Any]) -> bool:
    """True only with a bound endpoint AND a 64-hex workspace evidence hash."""
    endpoint = grants.get("endpoint_binding")
    evidence = grants.get("workspace_evidence_hash")
    if not isinstance(endpoint, str) or not endpoint.strip():
        return False
    if not isinstance(evidence, str) or len(evidence) != 64:
        return False
    return all(c in "0123456789abcdef" for c in evidence)


def _tool_effect(tool: Any) -> str:
    """Read a tool's declared effect from its FastMCP ``meta``, then its tags.

    An undeclared legacy tool is treated as ``read``, consistent with
    ``_tool_profile`` treating it as Insights: the fail-closed guarantee that
    matters is that a ``confirmed_write`` is only ever reachable through an
    explicit declaration, never by omission.
    """
    meta = getattr(tool, "meta", None)
    if isinstance(meta, dict):
        effect = meta.get("effect")
        if isinstance(effect, str) and effect in EFFECTS:
            return effect
    for tag in getattr(tool, "tags", None) or ():
        if isinstance(tag, str) and tag.startswith("effect:"):
            candidate = tag.split(":", 1)[1]
            if candidate in EFFECTS:
                return candidate
    return "read"


def _tool_profile(tool: Any) -> str | None:
    """Read a tool's declared profile from its FastMCP ``meta``; ``None`` if absent.

    AD-43 (`mcp-tool-surface.md`) -- THE PERMISSIVE DEFAULT IS GONE. This function
    returned ``DEFAULT_PROFILE`` for an undeclared tool "so discovery keeps listing
    it". ``insights`` is the profile that is ALWAYS visible, so the filter bounded
    exactly half the surface: measured on the assembled catalog, **83 of the 92**
    tools a default host saw carried no declaration at all -- 39 of them named
    after a connector, twelve of them tools that WRITE.

    ``None`` now means undeclared, and an undeclared tool is in no caller's visible
    set, so it reaches neither discovery nor a call. That is a runtime backstop, not
    the guarantee: ``validate_catalog`` refuses to BOOT with an undeclared tool in
    the assembled catalog, so the case this branch handles should never ship.
    """
    meta = getattr(tool, "meta", None)
    if isinstance(meta, dict):
        profile = meta.get("profile")
        if isinstance(profile, str) and profile in PROFILES:
            return profile
    # Fall back to tags (``profile:<name>``) when meta is absent.
    for tag in getattr(tool, "tags", None) or ():
        if isinstance(tag, str) and tag.startswith("profile:"):
            candidate = tag.split(":", 1)[1]
            if candidate in PROFILES:
                return candidate
    return None  # undeclared -> visible to nobody (AD-43)


# ---------------------------------------------------------------------------
# Capability context resolution from the MCP token (source of grants).
# ---------------------------------------------------------------------------


def _identity() -> str:
    """Resolve the caller identity from the MCP token.

    Delegates to `core.mcp_scope.caller_identity` rather than reading the `sub`
    claim again: the audit trail this module writes must name the person the
    membership registry knows, or one act leaves two trails under two names.
    """
    from core.mcp_scope import caller_identity  # noqa: PLC0415

    return caller_identity()


def _capability_context() -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Return ``(identity, host_context, grants)`` for the current call.

    THE CLAIM IS A POINTER, NOT A GRANT (67-16). The token's ``capability_context``
    claim is read for ONE purpose: to name which ``app.mcp_capability_contexts``
    row this call runs under -- by ``capability_context_id``, or failing that by
    the ``endpoint_binding`` it was bound to. Every field that then decides
    visibility (``enabled_profiles``, ``endpoint_binding``,
    ``workspace_evidence_hash``, ``interactive_presence_evidence_hash``) and every
    field of the host context is read from THAT ROW, at THIS call, by
    ``core.mcp_attestation.attest_capability_context``. Nothing a host writes into
    its own claims survives into the grants.

    That is what makes revocation immediate: the row carries ``revoked_at``, the
    attestation query filters on it, and a cut context stops attesting at the very
    next call rather than at token expiry -- the rule
    ``render_shares.resolve_session`` and ``session_revocation`` already obey.

    Absent a token, a pointer, a live row, or a readable store, grants are empty
    and only Insights is visible -- fail closed on every branch.
    """
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    if token is None:
        return "anonymous", {}, {}
    identity = _identity()
    claims = token.claims or {}
    pointer = claims.get("capability_context")
    if isinstance(pointer, dict):
        context_id = pointer.get("capability_context_id") or pointer.get("id")
        endpoint_binding = pointer.get("endpoint_binding")
        row = _attest(
            identity,
            context_id=context_id if isinstance(context_id, str) else None,
            endpoint_binding=endpoint_binding if isinstance(endpoint_binding, str) else None,
        )
    else:
        # NO POINTER CLAIM, AND THERE NEVER IS ONE (2026-09-04). The token this
        # deployment verifies is a Google ID token (`TOOROW_JWT_ISSUER=
        # https://accounts.google.com`): nothing mints a `capability_context`
        # claim into it, so from 2026-08-17 to this day every host was Insights
        # only and `app.mcp_capability_contexts` held zero rows in production.
        # The pointer is DERIVED from what Google signed -- the audience the
        # token was issued for (the endpoint) and its subject (the principal) --
        # and resolves a row only when exactly one live binding carries that
        # pair. The grants are still the row's, read at this call; a revoked row
        # still stops attesting at the next call. `mcp-tool-surface.md`, amendment
        # of 2026-09-04.
        row = _attest(
            identity,
            context_id=None,
            endpoint_binding=_token_audience(claims),
            client_id=_token_subject(claims),
        )
    if row is None:
        return identity, {}, {}
    host_context = {
        k: row.get(k)
        for k in ("host", "workspace_id", "workspace_type", "client_id")
        if row.get(k) is not None
    }
    grants = {
        "attested_context_id": row.get("id"),
        "org_id": row.get("org_id"),
        "enabled_profiles": row.get("enabled_profiles"),
        "endpoint_binding": row.get("endpoint_binding"),
        "workspace_evidence_hash": row.get("workspace_evidence_hash"),
        "interactive_presence_evidence_hash": row.get("interactive_presence_evidence_hash"),
    }
    return identity, host_context, grants


def _token_audience(claims: dict[str, Any]) -> str | None:
    """The audience a verified token was issued for, as one exact string.

    RFC 7519 lets `aud` be a string or a list; a Google ID token carries the one
    OAuth client id it was minted for. A list with several entries is not one
    endpoint and resolves nothing.
    """
    aud = claims.get("aud")
    if isinstance(aud, list):
        aud = aud[0] if len(aud) == 1 else None
    return aud.strip() if isinstance(aud, str) and aud.strip() else None


def _token_subject(claims: dict[str, Any]) -> str | None:
    sub = claims.get("sub")
    return sub.strip() if isinstance(sub, str) and sub.strip() else None


def _attest(
    identity: str,
    *,
    context_id: str | None,
    endpoint_binding: str | None,
    client_id: str | None = None,
) -> dict[str, Any] | None:
    """One indexed probe against the live capability context; ``None`` on any doubt.

    THE CONNECTION IS ARMED FOR THE CALLER, since 2026-08-31, and the exemption it
    used to carry was false as written. ``test_mcp_surfaces_acquire_an_armed_
    connection.py`` declared this site as "before any caller identity has been
    resolved"; its only caller resolves the identity through
    ``core.mcp_scope.caller_identity`` on the line above and then called this on a
    bare ``get_connection()``. That is the sentence ``core/db.py`` names as the
    defect -- *"the authorization check usually opens, uses and closes its own,
    and the handler then opens a fresh unarmed one"* -- and the README's *"One
    authorization key"* says the database opens for a VERIFIED identity, never to
    look for one.

    Arming it is also a second barrier rather than only a tidier acquisition:
    ``app.mcp_capability_contexts`` carries ``org_id`` and its Epic-36 policy is
    ``epic36_is_org_member(org_id)`` (migration 273), so a pointer naming a
    context of an organization this caller is not a member of now attests to
    NOTHING instead of handing back its grants.

    Swallowing the exception is the fail-CLOSED branch, not a lenient one: the
    caller treats ``None`` as "no grants", which is Insights only. An attestation
    seam that raised into the middleware would be caught there and produce the
    same answer, one stack trace louder.
    """
    if not context_id and not endpoint_binding:
        return None
    try:
        from core.db import request_connection  # noqa: PLC0415
        from core.mcp_attestation import attest_capability_context  # noqa: PLC0415

        with request_connection(identity) as conn:
            return attest_capability_context(
                conn,
                context_id=context_id,
                endpoint_binding=endpoint_binding,
                client_id=client_id,
            )
    except Exception as exc:  # noqa: BLE001 -- unreadable store -> insights only.
        logger.warning("mcp_profiles: capability context attestation failed (%s)", exc)
        return None


# ---------------------------------------------------------------------------
# The FastMCP middleware -- filter discovery + deny hidden calls (AC2, AC6).
# ---------------------------------------------------------------------------


def build_middleware():
    """Return a ``CapabilityProfileMiddleware`` instance, or None if unavailable.

    Registered on the FastMCP app (after tracing) so EVERY ``list_tools`` and
    ``call_tool`` passes the capability filter. Returns None when the FastMCP
    middleware base is unimportable so app construction never fails -- mirrors
    ``tracing.build_middleware``.

    Verified against FastMCP 3.4.4: ``Middleware.on_list_tools(context, call_next)``
    returns a ``Sequence[Tool]`` we can filter, and ``Middleware.on_call_tool(context,
    call_next)`` sees ``context.message.name`` (``CallToolRequestParams.name``).
    """
    try:
        from fastmcp.server.middleware import Middleware  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.debug("mcp_profiles: FastMCP middleware base unavailable (%s)", exc)
        return None

    class CapabilityProfileMiddleware(Middleware):
        """Enforce capability profiles at discovery AND call time (fail closed).

        Discovery (``on_list_tools``): delegates to ``model_visible_tools`` -- a tool
        is kept when its declared profile is in the caller's ``visible_profiles``
        and a ``confirmed_write`` has proven interactive presence.

        APP-ONLY TOOLS ARE EXCLUDED, sauf deux. Ce paragraphe disait << app-only
        tools stay on the wire >>, c'est-a-dire l'INVERSE de la fonction qu'il
        delegue : `model_visible_tools` filtre `is_app_only_tool(tool)` et ne
        laisse passer que les membres de `HOST_ROUTED_APP_ONLY_TOOLS`
        (`app_read_result_slice`, `submit_feedback`) -- les deux dont un hote
        DOIT lire la declaration dans ``tools/list`` pour router un appel de
        widget. Corrige le 2026-08-22 (story 67.7) : un lecteur qui croyait ce
        paragraphe pensait tout outil app-only decouvrable, et aurait cherche
        ailleurs pourquoi le sien ne l'etait pas. Les autres restent enregistres
        et appelables par leur nom stable ; les Skills generees les excluent
        toutes par `is_app_only_tool`.

        Insights is the default; higher profiles appear only for an authenticated
        caller with an endpoint/workspace-bound, evidence-backed capability
        context (E36-FR06, AC2).

        Invocation (``on_call_tool``): the SAME visibility check runs before the tool
        executes, so a direct call to a hidden/forbidden tool is denied at call time
        even when discovery never listed it (AC6). Denial does not disclose the tool's
        existence -- a generic ``not_found``.
        """

        async def on_list_tools(self, context, call_next):
            tools = await call_next(context)
            try:
                identity, host_context, grants = _capability_context()
                allowed = visible_profiles(identity, host_context, grants)
                interactive = interactive_presence_verified(grants)
            except Exception as exc:  # noqa: BLE001 -- fail closed to Insights only.
                logger.debug("mcp_profiles: list_tools context failed (%s)", exc)
                allowed = frozenset({DEFAULT_PROFILE})
                interactive = False
            return model_visible_tools(tools, allowed=allowed, interactive=interactive)

        async def on_call_tool(self, context, call_next):
            msg = getattr(context, "message", None)
            tool_name = getattr(msg, "name", None)
            if tool_name is not None and tool_name in _REGISTRY.declarations:
                decl = _REGISTRY.declarations[tool_name]
                try:
                    identity, host_context, grants = _capability_context()
                    allowed = visible_profiles(identity, host_context, grants)
                    interactive = interactive_presence_verified(grants)
                except Exception as exc:  # noqa: BLE001 -- fail closed.
                    logger.debug("mcp_profiles: call_tool context failed (%s)", exc)
                    allowed = frozenset({DEFAULT_PROFILE})
                    interactive = False
                # The same check runs here, so a direct call to a tool discovery
                # never listed is denied too -- without disclosing that it exists.
                if decl.profile not in allowed or (
                    decl.effect == "confirmed_write" and not interactive
                ):
                    raise _tool_error(
                        "not_found",
                        "Tool not found.",
                    )
            # Story 50.6 -- THE model-channel budget, for every returning tool.
            # Deliberately NOT restricted to `_REGISTRY.declarations` like the
            # visibility check above: the budget is an AD-1 property of the wire,
            # not of a capability declaration, and the tools that were over budget
            # (`list_card_templates`, `get_daily_report`) are exactly the legacy
            # ones registered on plain `mcp.tool` with no declaration at all.
            result = await call_next(context)
            return enforce_result_model_channel(
                tool_name if isinstance(tool_name, str) and tool_name else "<unnamed tool>",
                result,
            )

    return CapabilityProfileMiddleware()


def _tool_error_payload(payload: dict[str, Any]):
    """Return a ToolError carrying a full structured refusal (code + measurements)."""
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps(payload))


def _tool_error(code: str, message: str):
    """Return a ToolError carrying the canonical ``{code, message}`` JSON."""
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))
