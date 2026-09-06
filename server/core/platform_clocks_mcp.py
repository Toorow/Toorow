"""The platform clocks, on the MCP surface -- read the drift, edit it, apply it, fire it.

WHY. `execution-substrate.md` (AD-36) makes Cloud Scheduler "the clock": five
jobs that are the ONLY reason anything happens when nobody is looking. Their
state lived exclusively behind `gcloud`, so an agent asked to validate a gate on
the platform could not answer "is the nightly dispatch armed, and does it still
point where we declared it?" without a shell and a Google credential. Jean,
2026-08-02: *"on doit avoir une sync entre l'interface et les cloud schedule."*
The objective behind it is narrower and sharper: an agent must be AUTONOMOUS to
validate platform gates.

WHAT THIS EXPOSES. Five tools, all under `profile="operations"` -- run and
dispatch management, which is exactly what the Operations profile owns; nothing
here approves, publishes, or moves a live pointer, so nothing here is Governance.

  * `list_platform_clocks`       -- every clock: declared, observed, verdict.
  * `get_platform_clock`         -- one clock, same model.
  * `set_platform_clock_cadence` -- edit the DECLARED cadence (Postgres only).
  * `apply_platform_clock`       -- push the declared clock into GCP, idempotent.
  * `run_platform_clock_now`     -- fire one clock immediately.

TWO GATES, AND THEY ARE NOT THE SAME GATE.

  1. `register_profiled` (AD-24). Every tool below is declared with its profile,
     its effect and its confirmation mode, so `CapabilityProfileMiddleware`
     filters discovery and denies a direct call. A bare `mcp.tool` would escape
     that middleware entirely -- which is not hypothetical: `submit_feedback` in
     `core/main.py` is registered bare and therefore carries no declaration at
     all, a hole `mcp_profiles._record_app_declaration` names in its own
     docstring. It is not reproduced here, and a test in
     `tests/core/test_platform_clocks_mcp.py` reads this file's source to keep it
     that way.

  2. A PLATFORM role, checked at call time. The profile controls who can SEE a
     tool; it says nothing about who may touch platform-wide infrastructure.
     These clocks have no organization in scope -- there is no project to be a
     member of -- so the gate is the same deny-by-default `TOOROW_SUPER_ADMINS`
     allow-list every other no-organization act uses (`admin_api.
     _enforce_platform_admin`, `super_admin.is_super_admin`). Refusal is
     `not_found`, never `forbidden`: a caller outside the allow-list learns
     nothing about what exists. Any failure of the check itself -- including a
     failure to import it -- also refuses. Fail-closed: an uncertainty at an
     authorization seam is never a yes.

THE THREE WRITES ALSO NAME THEIR TARGET TWICE. Each takes a `confirm` argument
that must equal the clock name. The declaration's `confirmation_mode="human"`
already requires proven interactive presence before the tool is even discoverable
(AD-27), but that is a property of the SESSION; the echo is a property of the
CALL, and it is what stops a model from firing platform-scale work as a side
effect of a plausible-looking plan. `run_platform_clock_now` in particular cannot
be undone once dispatched.

READS NEVER REPAIR. The two read tools compose `list_declared` + `observe` +
`reconcile` and stop there. Drift is rendered -- named, counted, attributed --
and it survives until somebody asks for it to be applied. That invariant lives in
`core/platform_clocks_read_model.py`, which is the ONE model both this surface and
the REST surface serialise, so the two can never describe the same clock
differently.

Conventions mirror `operations_mcp`: `from __future__ import annotations`, module
logger, lazy imports inside function bodies (no cycle with `core.main`), French
user-facing microcopy, ASCII-only source. No production identifier appears here:
project and region are read from the environment by the read model, and clock
names are opaque data from the declaration.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Envelope schema version. Local, like operations_mcp: this module never imports
# a private of core.main.
_SCHEMA_VERSION = "1"


def _envelope(data: dict) -> dict:
    """Build the canonical AD-1 structured_content envelope."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "meta": {"freshness": None, "provenance": None, "alerts": []},
        "data": data,
    }


def _tool_error(code: str, message: str):
    """Return a ToolError carrying the canonical ``{code, message}`` JSON."""
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _identity() -> str:
    """Resolve the caller identity from the MCP token (same pattern as get_card)."""
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def _result(summary: str, data: dict):
    """Build the dual-channel ToolResult (lean text summary + AD-1 envelope)."""
    from fastmcp.tools.tool import ToolResult  # noqa: PLC0415
    from mcp.types import TextContent  # noqa: PLC0415

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=_envelope(data),
    )


def _require_platform_role() -> str:
    """Return the caller identity, or refuse with ``not_found``. Fail-closed.

    The clocks are PLATFORM scope: membership of a project proves nothing about
    them. The allow-list is `TOOROW_SUPER_ADMINS`, deny-by-default, so an
    unconfigured deployment exposes these tools to NOBODY -- which is the correct
    posture for a surface whose write half restarts platform-wide work.

    A failure of the check itself refuses too. An authorization seam that cannot
    run has not granted anything.
    """
    identity = _identity()
    try:
        from core.super_admin import identity_is_super_admin  # noqa: PLC0415

        # THE one resolution (audit 12, P1-2). `_identity()` is the token `sub`;
        # in canonical mode that is a `person_<ULID>`, which no email allow-list
        # can ever match, so these five tools were gated by a check that always
        # refused. The resolver reads the verified email from the registry.
        allowed = identity_is_super_admin(identity)
    except Exception as exc:  # noqa: BLE001 -- fail closed, never unguarded.
        logger.error("platform_clocks_mcp: platform role check failed: %s", type(exc).__name__)
        raise _tool_error("not_found", "Tool not found.") from exc
    if not allowed:
        logger.warning("platform_clocks_mcp: platform_role_denied identity=%s", identity)
        raise _tool_error("not_found", "Tool not found.")
    return identity


def _checked_clock_name(clock_name: str | None) -> str:
    """Normalise a clock name or refuse. Opaque identifier, bounded shape."""
    from core.platform_clocks_read_model import valid_clock_name  # noqa: PLC0415

    checked = valid_clock_name(clock_name)
    if checked is None:
        raise _tool_error("invalid_param", "Invalid clock_name.")
    return checked


def _require_echo_confirmation(clock_name: str, confirm: str | None) -> None:
    """Refuse unless the caller echoed the clock name back.

    `confirmation_mode="human"` gates the SESSION; this gates the CALL. Editing,
    applying or firing a platform clock changes what the whole platform does at a
    given minute, and a run cannot be recalled once dispatched -- so the target is
    named twice, deliberately, and a mismatch is refused rather than corrected.
    """
    if (confirm or "").strip() != clock_name:
        raise _tool_error(
            "confirmation_required",
            "Action not confirmed: repeat the clock name in `confirm`.",
        )


def _summarize(state: dict) -> str:
    """One line an operator can read: how many clocks, how many out of sync."""
    observation = state.get("observation") or {}
    if not observation.get("reachable", False):
        return (
            f"{state.get('count', 0)} declared clock(s); GCP state NOT observed "
            f"({observation.get('error')}) -- verdicts unknown, no synchronisation proven."
        )
    out_of_sync = state.get("out_of_sync") or []
    if not out_of_sync:
        return f"{state.get('count', 0)} clock(s): all in sync with the declared cadence."
    return (
        f"{state.get('count', 0)} clock(s): {len(out_of_sync)} drifted "
        f"({', '.join(out_of_sync)})."
    )


def _collect(clock_name: str | None = None) -> dict:
    """Shared read path: platform role, then the one read model."""
    from core.db import get_connection  # noqa: PLC0415
    from core.platform_clocks_read_model import (  # noqa: PLC0415
        PlatformClockSeamError,
        collect_clock_states,
    )

    try:
        with get_connection() as conn:
            return collect_clock_states(conn, clock_name=clock_name)
    except PlatformClockSeamError as exc:
        logger.error("platform_clocks_mcp: seam unavailable: %s", exc)
        raise _tool_error("seam_unavailable", "Clock surface unavailable.") from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("platform_clocks_mcp: read failed: %s", type(exc).__name__)
        raise _tool_error("server_error", "Clock read unavailable.") from exc


# ---- Read: every clock, with its drift verdict ------------------------------
def list_platform_clocks():
    """Liste les HORLOGES DE PLATEFORME : declare, constate, verdict de derive.

    Une horloge = un job Cloud Scheduler (AD-36 : « the clock »). Chaque entree
    rend le `declared` (ce que la plateforme a decide), le `observed` (ce que
    GCP execute reellement) et le `verdict` : `in_sync`, `drifted`,
    `missing_in_gcp`, `unmanaged_in_gcp` ou `unknown`.

    LECTURE PURE : la derive est RENDUE, jamais corrigee en silence. Pour la
    corriger, appeler `apply_platform_clock` explicitement.

    Si GCP n'est pas observable (pas de credential, pas de reseau), tous les
    verdicts valent `unknown` et `observation.reachable` est faux -- jamais
    `in_sync` : une observation qui n'a pas tourne ne prouve aucune synchro.
    """
    _require_platform_role()
    state = _collect()
    return _result(_summarize(state), state)


# ---- Read: one clock -------------------------------------------------------
def get_platform_clock(clock_name: str):
    """Lit UNE horloge de plateforme : declare, constate, verdict de derive.

    Meme modele de lecture que `list_platform_clocks`, restreint a une
    horloge. Une horloge inconnue rend `not_found` -- ni le declare ni le
    constate ne la mentionnent.
    """
    _require_platform_role()
    checked = _checked_clock_name(clock_name)
    state = _collect(checked)
    if not state.get("clocks"):
        raise _tool_error("not_found", "Clock not found.")
    clock = state["clocks"][0]
    data = {**state, "clock": clock}
    summary = (
        f"Clock {checked!r}: verdict {clock['verdict']} "
        f"(observation {'ok' if state['observation']['reachable'] else 'unavailable'})."
    )
    return _result(summary, data)


# ---- Write: edit the declared cadence (Postgres only, no GCP call) ---------
def set_platform_clock_cadence(
    clock_name: str,
    confirm: str,
    schedule: str | None = None,
    timezone: str | None = None,
    paused: bool | None = None,
):
    """Modifie la CADENCE DECLAREE d'une horloge. N'ecrit PAS encore dans GCP.

    - `schedule` : expression cron de la cadence declaree.
    - `timezone` : fuseau de reference de cette cadence.
    - `paused`   : suspendre / reprendre l'horloge declaree.
    - `confirm`  : repeter `clock_name` -- l'action repointe la plateforme.

    Apres cet appel, l'horloge lit `drifted` tant que
    `apply_platform_clock` n'a pas ete appele : le declare et l'infrastructure
    sont DEUX faits, et c'est precisement ce que cette surface existe pour
    rendre lisible.
    """
    identity = _require_platform_role()
    checked = _checked_clock_name(clock_name)
    _require_echo_confirmation(checked, confirm)
    if schedule is None and timezone is None and paused is None:
        raise _tool_error(
            "invalid_param",
            "Nothing to change: provide schedule, timezone or paused.",
        )

    from core.db import get_connection  # noqa: PLC0415
    from core.platform_clocks_read_model import (  # noqa: PLC0415
        PlatformClockSeamError,
        set_declared_cadence,
    )

    try:
        with get_connection() as conn:
            result = set_declared_cadence(
                conn,
                clock_name=checked,
                actor=identity,
                schedule=schedule,
                timezone=timezone,
                paused=paused,
            )
    except PlatformClockSeamError as exc:
        logger.error("platform_clocks_mcp: cadence seam unavailable: %s", exc)
        raise _tool_error("seam_unavailable", "Cadence edit unavailable.") from exc
    except ValueError as exc:
        raise _tool_error("invalid_cadence", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("platform_clocks_mcp: cadence write failed: %s", type(exc).__name__)
        raise _tool_error("server_error", "Cadence edit unavailable.") from exc

    summary = (
        f"Declared cadence of {checked!r} changed. Not applied in GCP: "
        "call apply_platform_clock to close the drift."
    )
    return _result(summary, result)


# ---- Write: apply the declared clock into GCP (idempotent) -----------------
def apply_platform_clock(clock_name: str, confirm: str):
    """Applique le DECLARE dans GCP pour une horloge (create/update/pause/resume).

    Idempotent : reappliquer une horloge deja en phase ne change rien. Rend
    l'etat RECOLLECTE apres l'application, donc le verdict effectivement
    obtenu -- pas celui espere. `confirm` doit repeter `clock_name` : cette
    action touche l'infrastructure d'execution de la plateforme.
    """
    identity = _require_platform_role()
    checked = _checked_clock_name(clock_name)
    _require_echo_confirmation(checked, confirm)

    from core.db import get_connection  # noqa: PLC0415
    from core.platform_clocks_read_model import (  # noqa: PLC0415
        PlatformClockSeamError,
        apply_declared,
    )

    try:
        with get_connection() as conn:
            result = apply_declared(conn, clock_name=checked, actor=identity)
    except PlatformClockSeamError as exc:
        logger.error("platform_clocks_mcp: apply seam unavailable: %s", exc)
        raise _tool_error("seam_unavailable", "Apply unavailable.") from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("platform_clocks_mcp: apply failed: %s", type(exc).__name__)
        raise _tool_error("server_error", "Apply unavailable.") from exc

    state = result.get("state") or {}
    clocks = state.get("clocks") or [{}]
    summary = (
        f"Clock {checked!r} applied in GCP: resulting verdict "
        f"{clocks[0].get('verdict', 'unknown')}."
    )
    return _result(summary, result)


# ---- Write: fire one clock immediately -------------------------------------
def run_platform_clock_now(clock_name: str, confirm: str):
    """Declenche UNE horloge immediatement (execution hors cadence).

    Ce n'est PAS une lecture et ce n'est pas annulable : l'invocation part et
    le travail de plateforme demarre. `confirm` doit repeter `clock_name`.

    Ne modifie ni la cadence declaree ni l'horloge dans GCP : c'est une
    execution ponctuelle, la prochaine execution planifiee reste inchangee.
    """
    identity = _require_platform_role()
    checked = _checked_clock_name(clock_name)
    _require_echo_confirmation(checked, confirm)

    from core.db import get_connection  # noqa: PLC0415
    from core.platform_clocks_read_model import (  # noqa: PLC0415
        PlatformClockSeamError,
        run_clock_now,
    )

    try:
        with get_connection() as conn:
            result = run_clock_now(conn, clock_name=checked, actor=identity)
    except PlatformClockSeamError as exc:
        logger.error("platform_clocks_mcp: run seam unavailable: %s", exc)
        raise _tool_error("seam_unavailable", "Trigger unavailable.") from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("platform_clocks_mcp: run failed: %s", type(exc).__name__)
        raise _tool_error("server_error", "Trigger unavailable.") from exc

    summary = f"Clock {checked!r} triggered immediately."
    return _result(summary, result)


def register(mcp) -> None:
    """Register the platform-clock tools on *mcp*.

    Called once from `core.main`, before `validate_catalog()` so the boot-time
    validator sees these declarations. Reads are `effect="read"` /
    `confirmation_mode="none"`; the three writes are `effect="confirmed_write"` /
    `confirmation_mode="human"` -- editing a cadence REPOINTS the clock, applying
    it MUTATES infrastructure, and running it DISPATCHES platform-scale work.
    None of the three is a preference toggle, and none of them is a read.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    # ---- Declarations. Every tool goes through register_profiled -- a bare
    # ---- mcp.tool would escape the capability middleware entirely.
    register_profiled(
        mcp,
        list_platform_clocks,
        profile="operations",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        get_platform_clock,
        profile="operations",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        set_platform_clock_cadence,
        profile="operations",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
    register_profiled(
        mcp,
        apply_platform_clock,
        profile="operations",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
    register_profiled(
        mcp,
        run_platform_clock_now,
        profile="operations",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
