"""One read model of the PLATFORM CLOCKS, serialised by two surfaces (MCP + REST).

WHY THIS FILE EXISTS AT ALL.

`execution-substrate.md` (AD-36) names Cloud Scheduler "the clock": the only part
of the product that must run when nobody is there. Until now that clock was
readable in exactly one place -- `gcloud scheduler jobs list` -- so an agent asked
to validate a gate on the platform had no way to answer "is the nightly dispatch
actually armed?" without a shell and a Google credential. Jean, 2026-08-02:
*"on doit avoir une sync entre l'interface et les cloud schedule."*

`core/platform_clocks.py` (sibling module) owns the mechanics: what is DECLARED,
what is OBSERVED in GCP, the per-clock drift verdict, the idempotent apply, and
the immediate run. This module owns NEITHER the mechanics nor a second copy of
them -- it composes them into ONE serialisable read model.

That single composition is the point. `inbound_health_api` states the rule this
follows: *"a handler that reshapes is a handler that can leak something the read
model was careful to exclude, and it is how REST and MCP drift into describing the
same delivery differently."* A clock surface that described drift one way to a
console and another way to an agent would be worse than no surface: two answers to
"is it in sync?" is the same as none.

THE INVARIANT THIS MODULE ENFORCES, AND THE ONLY ONE IT OWNS:

    reading NEVER repairs.

`collect_clock_states` calls `list_declared`, `observe` and `reconcile`. It does
not call `apply`, and it does not call `run_now`. A drift is RENDERED -- named,
counted, and attributed to a clock -- and it stays until somebody with a platform
role asks for it to be applied. Silent convergence would make the drift
unobservable, which is `Incomplete if` #2 of the substrate page ("a scheduled run
that does not happen leaves no record that it did not happen") wearing a different
hat.

AND WHEN GCP CANNOT BE REACHED, THE VERDICT IS `unknown`, NEVER `in_sync`. An
observation that could not run has not proven synchronisation; reporting the
declared row alone as healthy is precisely the `missed_run_count = 0` failure the
substrate page was written about.

No production identifier is hard-coded here: project and region are read from the
environment, and the clock names arrive as opaque data from the declaration.
"""

from __future__ import annotations

import inspect
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

#: The verdict vocabulary `core.platform_clocks.reconcile` returns. Anything
#: outside this set is normalised to `unknown` rather than passed through: a
#: verdict this module cannot interpret must not be shown as a healthy one.
DRIFT_VERDICTS: tuple[str, ...] = (
    "in_sync",
    "drifted",
    "missing_in_gcp",
    "unmanaged_in_gcp",
    "unknown",
)

UNKNOWN_VERDICT = "unknown"

#: A clock name is an opaque, bounded identifier. Validated before it reaches the
#: seam so a malformed name is a 400/`invalid_param` here rather than an opaque
#: provider error three layers down.
CLOCK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

#: Candidate function names for "write the DECLARED cadence". The dependency
#: contract handed to this story names five functions -- `list_declared`,
#: `observe`, `reconcile`, `apply`, `run_now` -- and editing the declaration is
#: not among them, yet the surface is required to offer it. Rather than invent a
#: second declaration store (which would be a second source of truth for when the
#: platform runs), this resolves the sibling's own writer by name and FAILS LOUDLY
#: when none exists. A missing writer is a seam gap to reconcile, not a silent
#: no-op that looks like a successful edit.
_DECLARED_WRITER_NAMES: tuple[str, ...] = (
    "set_declared",
    "update_declared",
    "set_declared_cadence",
    "upsert_declared",
    "declare",
)

#: Parameter aliases, so this module survives a reasonable naming choice on the
#: other side of the seam without either side rewriting the other. Keys are the
#: canonical names used in THIS file; values are the parameter names accepted.
_ALIASES: dict[str, tuple[str, ...]] = {
    "conn": ("conn", "connection", "db"),
    "declared": ("declared", "declarations"),
    "observed": ("observed", "observation"),
    "clock_name": ("clock_name", "name", "job", "job_name"),
    "schedule": ("schedule", "cadence", "cron"),
    "timezone": ("timezone", "time_zone", "tz"),
    "paused": ("paused", "pause"),
    "actor": ("actor", "identity"),
    "project": ("project", "project_id", "gcp_project"),
    "region": ("region", "location"),
}

#: Keys under which a name may travel inside one declared/observed entry.
_NAME_KEYS: tuple[str, ...] = ("clock_name", "name", "job_name", "id")

#: Keys under which a verdict may travel inside one reconcile entry.
_VERDICT_KEYS: tuple[str, ...] = ("verdict", "drift", "state", "status")

#: Keys under which a list of entries may travel inside an envelope.
_ENVELOPE_KEYS: tuple[str, ...] = ("clocks", "items", "jobs", "verdicts", "results")

_MISSING = object()


class PlatformClockSeamError(RuntimeError):
    """The `core.platform_clocks` seam cannot be called with what we can offer.

    Raised rather than swallowed on purpose: a surface that quietly skipped an
    unreachable seam would report an empty clock list, and an empty list reads
    exactly like "no clocks are declared" -- the one answer that must never be
    guessed.
    """


def _seam():
    """Import the sibling module lazily (no import cycle, and testable)."""
    from core import platform_clocks  # noqa: PLC0415

    return platform_clocks


def _match(param_name: str, canonical: dict[str, Any]):
    """Resolve one seam parameter name against the canonical values we hold."""
    if param_name in canonical:
        return canonical[param_name]
    for key, aliases in _ALIASES.items():
        if param_name in aliases and key in canonical:
            return canonical[key]
    return _MISSING


def _call(fn, **canonical):
    """Call *fn* with the arguments it declares, and refuse when one is missing.

    The sibling module is written in parallel with this one, so its exact keyword
    spelling is not frozen. This adapter binds what the function actually accepts
    and raises `PlatformClockSeamError` when a REQUIRED parameter cannot be
    supplied. It deliberately does not "best effort" a partial call: a reconcile
    run without the connection it asked for would return a verdict computed
    against nothing.
    """
    parameters = inspect.signature(fn).parameters
    accepts_kwargs = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
    )
    args: list[Any] = []
    kwargs: dict[str, Any] = dict(canonical) if accepts_kwargs else {}

    for name, param in parameters.items():
        if param.kind is not inspect.Parameter.POSITIONAL_ONLY:
            continue
        value = _match(name, canonical)
        if value is _MISSING:
            if param.default is inspect.Parameter.empty:
                raise PlatformClockSeamError(
                    f"{getattr(fn, '__name__', fn)!r} needs positional {name!r}, "
                    "which this surface cannot supply"
                )
            break
        args.append(value)

    bindable = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    for name, param in parameters.items():
        if param.kind not in bindable or name in kwargs:
            continue
        value = _match(name, canonical)
        if value is not _MISSING:
            kwargs[name] = value

    missing = [
        name
        for name, param in parameters.items()
        if param.kind in bindable
        and param.default is inspect.Parameter.empty
        and name not in kwargs
    ]
    if missing:
        raise PlatformClockSeamError(
            f"{getattr(fn, '__name__', fn)!r} requires {missing}, which this "
            "surface cannot supply"
        )
    return fn(*args, **kwargs)


def _seam_function(seam, names: tuple[str, ...]):
    """Return the first callable among *names*, or raise naming all of them."""
    for name in names:
        candidate = getattr(seam, name, None)
        if callable(candidate):
            return candidate
    raise PlatformClockSeamError(
        "core.platform_clocks exposes none of " + ", ".join(names)
    )


def _environment_context() -> dict[str, Any]:
    """Project / region from the ENVIRONMENT -- never a literal in this repository.

    Omitted entirely when unset, so the seam applies its own default rather than
    receiving an empty string that reads like an explicit choice.
    """
    context: dict[str, Any] = {}
    project = (
        os.environ.get("TOOROW_GCP_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or ""
    ).strip()
    region = (
        os.environ.get("TOOROW_GCP_REGION")
        or os.environ.get("GOOGLE_CLOUD_REGION")
        or ""
    ).strip()
    if project:
        context["project"] = project
    if region:
        context["region"] = region
    return context


def _jsonable(value: Any) -> Any:
    """Make a seam payload safe for `JSONResponse` / an MCP envelope.

    Timestamps are the reason: a declaration or an observation carries
    `last_attempt_time`, and a `datetime` in a JSONResponse is a 500 on a surface
    whose whole job is to be readable when something is already wrong.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(value)


def _name_of(entry: Any) -> str | None:
    if isinstance(entry, dict):
        for key in _NAME_KEYS:
            candidate = entry.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _index(value: Any) -> dict[str, Any]:
    """Index a declared/observed payload by clock name, whatever shape it takes."""
    if value is None:
        return {}
    if isinstance(value, dict):
        for key in _ENVELOPE_KEYS:
            inner = value.get(key)
            if isinstance(inner, list):
                return _index(inner)
        indexed: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.strip():
                indexed[key.strip()] = item if isinstance(item, dict) else {"value": item}
        return indexed
    if isinstance(value, (list, tuple)):
        indexed = {}
        for entry in value:
            name = _name_of(entry)
            if name:
                indexed[name] = entry
        return indexed
    return {}


def _verdict_of(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in _VERDICT_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _verdict_index(value: Any) -> dict[str, str]:
    """Index reconcile output as ``{clock_name: verdict}``."""
    verdicts: dict[str, str] = {}
    if value is None:
        return verdicts
    if isinstance(value, dict):
        for key in _ENVELOPE_KEYS:
            inner = value.get(key)
            if isinstance(inner, list):
                return _verdict_index(inner)
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                continue
            verdict = _verdict_of(item)
            if verdict:
                verdicts[key.strip()] = verdict
        return verdicts
    if isinstance(value, (list, tuple)):
        for entry in value:
            name = _name_of(entry)
            verdict = _verdict_of(entry)
            if name and verdict:
                verdicts[name] = verdict
    return verdicts


def valid_clock_name(clock_name: str | None) -> str | None:
    """Return the normalised clock name, or None when it is not a plausible one."""
    candidate = (clock_name or "").strip()
    if not candidate or not CLOCK_NAME_RE.match(candidate):
        return None
    return candidate


def collect_clock_states(conn, *, clock_name: str | None = None) -> dict[str, Any]:
    """Declared + observed + drift verdict for every platform clock. READ ONLY.

    Calls `list_declared`, then `observe`, then `reconcile`. It never calls
    `apply` and never calls `run_now`: see the module docstring -- a read that
    converges is a read that erases the evidence it was opened to show.

    When `observe` fails (no credential, no network, GCP refusing), the failure is
    reported under `observation.error` and EVERY verdict becomes `unknown`. That
    is not degradation: `in_sync` would be a claim nothing supports.
    """
    seam = _seam()
    declared_raw = seam.list_declared(conn)
    declared = _index(declared_raw)

    environment = _environment_context()
    observation_error: str | None = None
    observed_raw: Any = None
    try:
        observed_raw = _call(
            seam.observe, conn=conn, declared=declared_raw, **environment
        )
    except Exception as exc:  # noqa: BLE001 -- unknown, never optimistic.
        observation_error = type(exc).__name__
        logger.error("platform_clocks: observe failed: %s", observation_error)
    observed = _index(observed_raw)

    reconcile_error: str | None = None
    verdicts: dict[str, str] = {}
    if observation_error is None:
        try:
            verdicts = _verdict_index(
                _call(
                    seam.reconcile,
                    conn=conn,
                    declared=declared_raw,
                    observed=observed_raw,
                    **environment,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- unknown, never optimistic.
            reconcile_error = type(exc).__name__
            logger.error("platform_clocks: reconcile failed: %s", reconcile_error)

    names = sorted(set(declared) | set(observed))
    if clock_name is not None:
        names = [name for name in names if name == clock_name]

    clocks: list[dict[str, Any]] = []
    for name in names:
        verdict = verdicts.get(name)
        if verdict not in DRIFT_VERDICTS:
            verdict = UNKNOWN_VERDICT
        clocks.append(
            {
                "clock_name": name,
                "declared": _jsonable(declared.get(name)),
                "observed": _jsonable(observed.get(name)),
                "verdict": verdict,
                "in_sync": verdict == "in_sync",
            }
        )

    verdict_counts: dict[str, int] = {}
    for clock in clocks:
        verdict_counts[clock["verdict"]] = verdict_counts.get(clock["verdict"], 0) + 1

    return {
        "clocks": clocks,
        "count": len(clocks),
        "out_of_sync": [c["clock_name"] for c in clocks if not c["in_sync"]],
        "verdict_counts": verdict_counts,
        "observation": {
            "reachable": observation_error is None,
            "error": observation_error,
            "reconcile_error": reconcile_error,
        },
        # Stated in the payload, not only in a docstring: whoever reads this
        # response must be able to see that nothing was repaired on their behalf.
        "drift_is_reported_not_repaired": True,
    }


def set_declared_cadence(
    conn,
    *,
    clock_name: str,
    actor: str,
    schedule: str | None = None,
    timezone: str | None = None,
    paused: bool | None = None,
) -> dict[str, Any]:
    """Write the DECLARED cadence of one clock. GCP is untouched until apply.

    Returns the refreshed single-clock state, which after a real edit reads
    `drifted` -- that is correct and deliberate. The declaration and the running
    infrastructure are two facts, and pretending an edit reached Cloud Scheduler
    because it reached Postgres is exactly the confusion this surface exists to
    remove.
    """
    seam = _seam()
    writer = _seam_function(seam, _DECLARED_WRITER_NAMES)
    canonical: dict[str, Any] = {
        "conn": conn,
        "clock_name": clock_name,
        "actor": actor,
    }
    if schedule is not None:
        canonical["schedule"] = schedule
    if timezone is not None:
        canonical["timezone"] = timezone
    if paused is not None:
        canonical["paused"] = bool(paused)
    outcome = _call(writer, **canonical)
    return {
        "clock_name": clock_name,
        "outcome": _jsonable(outcome),
        "applied_to_gcp": False,
        "state": collect_clock_states(conn, clock_name=clock_name),
    }


def apply_declared(conn, *, clock_name: str, actor: str) -> dict[str, Any]:
    """Push the DECLARED clock into GCP (create/update/pause/resume). Idempotent.

    The post-state is re-collected and returned, so the caller sees the verdict
    that resulted rather than the verdict that was hoped for.
    """
    seam = _seam()
    outcome = _call(
        seam.apply,
        conn=conn,
        clock_name=clock_name,
        actor=actor,
        **_environment_context(),
    )
    return {
        "clock_name": clock_name,
        "outcome": _jsonable(outcome),
        "state": collect_clock_states(conn, clock_name=clock_name),
    }


def run_clock_now(conn, *, clock_name: str, actor: str) -> dict[str, Any]:
    """Fire one clock immediately. Not a read, and not undoable once dispatched."""
    seam = _seam()
    outcome = _call(
        seam.run_now,
        conn=conn,
        clock_name=clock_name,
        actor=actor,
        **_environment_context(),
    )
    return {"clock_name": clock_name, "outcome": _jsonable(outcome)}


#: How many nights the surface asks for. One is the operator's question ("what
#: happened last night?"); the cap exists so a mis-typed query cannot ask the
#: console to render a year.
DEFAULT_NIGHTS = 1
MAX_NIGHTS = 14


def collect_nightly_step_runs(conn, *, nights: int = DEFAULT_NIGHTS) -> dict[str, Any]:
    """The nightly STEP ledger, serialised. READ ONLY, and it repairs nothing.

    `execution-substrate.md` "Incomplete if" 2 has three loci, and this is the
    third: a nightly step that silently never ran. The clock registry above says
    whether the heartbeat fired; this says whether the work inside it happened.
    Both are read here so an operator asking the second question does not have to
    find a second screen.

    The seam derives every state, including the two absences (`never_started`,
    `unrecorded`) and the open row (`unfinished`). Nothing is re-derived here:
    a surface that recomputed a state would be the second answer to a question
    this module exists to answer once.
    """
    seam = _seam()
    bounded = max(1, min(MAX_NIGHTS, int(nights)))
    return _jsonable(seam.list_nightly_step_runs(conn, nights=bounded))
