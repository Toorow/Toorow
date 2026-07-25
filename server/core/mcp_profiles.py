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

Where host/workspace cryptographic proof is not yet integrated with a real host,
the capability context carries no ``workspace_evidence_hash`` and high-risk
profiles simply stay unavailable (fail closed). That is correct behaviour: no host
self-report can raise a profile above ``insights`` without server-verifiable proof.

Mirrors the metric_semantics_mcp conventions: ``from __future__ import
annotations``, module logger, lazy ``fastmcp`` imports inside function bodies (no
import cycle with core.main), French user-facing error microcopy, ASCII-only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
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

EFFECTS: tuple[str, ...] = ("read", "write")

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


@dataclass(frozen=True)
class ToolDeclaration:
    """One immutable capability declaration recorded at registration time."""

    name: str
    profile: str
    effect: str
    data_class: str
    confirmation_mode: str

    def connection_class(self) -> str:
        """"read" for insights tools, "admin" for every opt-in profile."""
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
    _REGISTRY.record(decl)
    mcp.tool(
        handler,
        name=name,
        tags={
            f"profile:{profile}",
            f"effect:{effect}",
            f"data_class:{data_class}",
        },
        meta={
            "profile": profile,
            "effect": effect,
            "data_class": data_class,
            "confirmation_mode": confirmation_mode,
        },
    )
    return handler


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

    Two invariants:
      * an ``insights`` tool must be ``effect=read`` (Insights is safe reads only);
      * a ``read`` tool never demands human/host in-app confirmation -- confirmation
        modes above ``none`` only make sense for a consequential write.
    """
    if decl.profile == DEFAULT_PROFILE and decl.effect != "read":
        raise CatalogValidationError(
            f"insights tool {decl.name!r} must be effect=read (safe reads only)"
        )
    if decl.effect == "read" and decl.confirmation_mode != "none":
        raise CatalogValidationError(
            f"read tool {decl.name!r} cannot require confirmation_mode="
            f"{decl.confirmation_mode!r}"
        )


# ---------------------------------------------------------------------------
# Startup validation -- fail closed at boot (AC1).
# ---------------------------------------------------------------------------


def validate_catalog() -> tuple[ToolDeclaration, ...]:
    """Validate every registered tool at boot; raise on any gap/contradiction.

    Guarantees, before the server accepts a single call:
      * every tool declares a VALID profile/effect/data_class/confirmation_mode;
      * no declaration is self-contradictory (``_assert_consistent``);
      * every ``insights`` tool is ``effect=read`` (redundant safety net for AC1).

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
            raise CatalogValidationError(
                f"insights tool {decl.name!r} declares a write effect"
            )
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
      * the capability context (``grants``) explicitly enables that profile via
        ``enabled_profiles`` (policy opt-in, resolved upstream from the shared
        access seam -- this module never re-derives org/project rights);
      * the context is bound to a verified endpoint/workspace: a non-empty
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
    # FAIL-CLOSED trust boundary (review C1): the endpoint_binding/workspace_evidence
    # here arrive via the caller's capability context and are only shape-checked, not
    # yet verified server-side against the immutable app.mcp_capability_contexts row
    # that Story 36.14 binds (no host attestation callback exists yet). Until that
    # per-context server verification lands, a self-reported claim must NOT unlock
    # Operations/Governance/Support -- consistent with Story 36.1 AC6 (production
    # profiles stay disabled until the gates pass). A deployment that has wired real
    # host/workspace proof opts in explicitly via TOOROW_MCP_HIGHRISK_ENABLED.
    if not high_risk_profiles_enabled():
        return frozenset(visible)  # high-risk gated off -> insights only
    for profile in enabled:
        if profile in _HIGH_RISK_PROFILES:
            visible.add(profile)
    return frozenset(visible)


def high_risk_profiles_enabled() -> bool:
    """True only when a deployment has explicitly enabled high-risk MCP profiles.

    Defaults OFF so a forgeable/self-reported capability claim can never expose
    Operations/Governance/Support before server-side host/workspace proof exists.
    """
    return os.environ.get("TOOROW_MCP_HIGHRISK_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "on",
        "yes",
    }


def _endpoint_workspace_verified(grants: dict[str, Any]) -> bool:
    """True only with a bound endpoint AND a 64-hex workspace evidence hash."""
    endpoint = grants.get("endpoint_binding")
    evidence = grants.get("workspace_evidence_hash")
    if not isinstance(endpoint, str) or not endpoint.strip():
        return False
    if not isinstance(evidence, str) or len(evidence) != 64:
        return False
    return all(c in "0123456789abcdef" for c in evidence)


def _tool_profile(tool: Any) -> str:
    """Read a tool's declared profile from its FastMCP ``meta``.

    Governed high-risk tools (operations/governance/support) are ALWAYS declared
    via ``register_profiled`` and carry an explicit ``meta.profile``. A tool with
    no declared profile is a legacy safe-read (the dozens of Epic 1-35 tools
    registered via plain ``mcp.tool``): it is treated as the Insights default so
    discovery keeps listing it. This is consistent with ``on_call_tool``, which
    only denies REGISTERED tools -- an undeclared tool passes through both hooks.
    The fail-closed guarantee that matters for Epic 36 is preserved: a high-risk
    profile is reachable ONLY through an explicit declaration + endpoint/workspace
    opt-in, never by omission.
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
    return DEFAULT_PROFILE  # undeclared legacy tool -> Insights default (visible)


# ---------------------------------------------------------------------------
# Capability context resolution from the MCP token (source of grants).
# ---------------------------------------------------------------------------


def _identity() -> str:
    """Resolve the caller identity from the MCP token (metric_semantics pattern)."""
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def _capability_context() -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Return ``(identity, host_context, grants)`` for the current call.

    ``grants`` carries ``enabled_profiles``/``endpoint_binding``/
    ``workspace_evidence_hash`` sourced ONLY from a server-verified capability
    context (the ``app.mcp_capability_contexts`` row bound to this endpoint). Absent
    a token or a bound context, grants are empty and only Insights is visible -- fail
    closed. This module never trusts host self-report for these fields.
    """
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    if token is None:
        return "anonymous", {}, {}
    identity = token.claims.get("sub", token.client_id)
    claims = token.claims or {}
    context = claims.get("capability_context")
    if not isinstance(context, dict):
        return identity, {}, {}
    host_context = {
        k: context.get(k)
        for k in ("host", "workspace_id", "workspace_type", "client_id")
        if context.get(k) is not None
    }
    grants = {
        "enabled_profiles": context.get("enabled_profiles"),
        "endpoint_binding": context.get("endpoint_binding"),
        "workspace_evidence_hash": context.get("workspace_evidence_hash"),
    }
    return identity, host_context, grants


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

        Discovery (``on_list_tools``): a tool is kept only when its declared profile
        is in the caller's ``visible_profiles``. Insights is the default; higher
        profiles appear only for an authenticated caller with an endpoint/workspace-
        bound, evidence-backed capability context (E36-FR06, AC2).

        Invocation (``on_call_tool``): the SAME visibility check runs before the tool
        executes, so a direct call to a hidden/forbidden tool is denied at call time
        even when discovery never listed it (AC6). Denial does not disclose the tool's
        existence -- a generic ``not_found`` in French.
        """

        async def on_list_tools(self, context, call_next):
            tools = await call_next(context)
            try:
                identity, host_context, grants = _capability_context()
                allowed = visible_profiles(identity, host_context, grants)
            except Exception as exc:  # noqa: BLE001 -- fail closed to Insights only.
                logger.debug("mcp_profiles: list_tools context failed (%s)", exc)
                allowed = frozenset({DEFAULT_PROFILE})
            return [tool for tool in tools if _tool_profile(tool) in allowed]

        async def on_call_tool(self, context, call_next):
            msg = getattr(context, "message", None)
            tool_name = getattr(msg, "name", None)
            if tool_name is not None and tool_name in _REGISTRY.declarations:
                decl = _REGISTRY.declarations[tool_name]
                try:
                    identity, host_context, grants = _capability_context()
                    allowed = visible_profiles(identity, host_context, grants)
                except Exception as exc:  # noqa: BLE001 -- fail closed.
                    logger.debug("mcp_profiles: call_tool context failed (%s)", exc)
                    allowed = frozenset({DEFAULT_PROFILE})
                if decl.profile not in allowed:
                    raise _tool_error(
                        "not_found",
                        "Outil introuvable.",
                    )
            return await call_next(context)

    return CapabilityProfileMiddleware()


def _tool_error(code: str, message: str):
    """Return a ToolError carrying the canonical ``{code, message}`` JSON (French)."""
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))
