"""The ONE place that knows WHY an execution exists -- story 63.7.

WHY THIS MODULE EXISTS. `app.datastream_executions` is the registry of every
long treatment of a Datastream, and five different paths mint one: the nightly
and hourly collections, a day re-collection, a mapping or plan change, and the
first candidate of a Datastream (setup, or a Country activation). Measured
2026-08-06, the console could not tell them apart -- and neither could the MCP:

  * `execution_progress.open_collection_run` carried an `origin`, written as a
    BARE LITERAL at each of its three call sites (`"scheduler_nightly"`,
    `"scheduler_hourly"`, `"refetch"`), with nothing declaring what the
    vocabulary was;
  * `bounded_recovery` wrote `recovery_kind` -- a fourth word for the same
    question;
  * `datastream_change`, `datastream_first_candidate`, `datastream_activation`
    and `country_activation` wrote NOTHING at all;
  * and `datastream_workbench` did `run.pop("projection_plan_ref")`, so even the
    one path that named itself never reached a screen.

A treatment whose origin has been forgotten reads as an anomaly. That is the
whole of story 63.7, and this table is what makes the answer one answer.

WHY `has_engine` IS A FIELD OF THIS TABLE AND NOT A NOTE SOMEWHERE. An execution
that nothing can advance is not a harmless orphan. `uq_datastream_executions_
active` allows AT MOST ONE non-terminal execution per Datastream, so a run left
in `created` answers 409 to every later publication AND makes
`open_collection_run` return `None` every following night -- the flux loses its
progression, permanently, and nothing on any screen says why. Measured
2026-08-06: `bounded_recovery._dispatch_bounded_recovery` minted exactly that,
with no `enqueue`, no `advance_state`, and no worker reading its
`recovery_kind`; `operations_mcp._dispatch_recovery` did the same for its
`retry` / `refetch` verbs.

So an origin declares whether this build has anything that would carry its
execution to a terminal state. `has_engine=False` means ONE thing: refuse the
gesture, with the sentence that says why, and mint nothing.
`stamp_origin` is what enforces it -- a writer cannot stamp an origin whose
engine does not exist, and `tests/conformance/test_run_origin_registry.py`
refuses a writer that invents a literal instead of stamping.

BUILDING THAT ENGINE IS A DIFFERENT STORY. Nothing here says the three verbs are
wrong; it says they are not built, and that refusing is what an unbuilt verb
does.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class RunOriginError(ValueError):
    """An origin no build knows, or one whose execution nothing would advance."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RunOrigin:
    """One reason an execution exists, and every question a reader may ask."""

    #: What is written into `projection_plan_ref.origin`. Never shown to a person.
    key: str
    #: The English words a person reads. The console mirrors this string exactly.
    label: str
    #: Does a run of this origin pull windows from a provider? When it does not,
    #: `windows_total` is `NULL` because the run declared no window -- not
    #: because a measurement failed, and never because it is at 0 %.
    reads_provider_windows: bool
    #: Does ANYTHING in this build move an execution of this origin to a terminal
    #: state? `False` means the gesture refuses instead of minting one.
    has_engine: bool


def _o(key, **kwargs) -> RunOrigin:
    return RunOrigin(key=key, **kwargs)


#: Every origin, in the order the paths were measured in story 63.7 section 1.
#: KEEP THIS AND `ui/admin/src/datastreams/workbench/runOrigins.ts` IN STEP --
#: the conformance test compares them entry by entry.
RUN_ORIGINS: tuple[RunOrigin, ...] = (
    # --- The collections. They declare windows, so they have days to count. ---
    _o("scheduler_nightly", label="Nightly collection",
       reads_provider_windows=True, has_engine=True),
    _o("scheduler_hourly", label="Hourly collection",
       reads_provider_windows=True, has_engine=True),
    _o("refetch", label="Day re-collection",
       reads_provider_windows=True, has_engine=True),
    # A run someone asked for by hand. It is the SAME run the clock would have
    # started -- same gates, same window, same queue (`core.datastream_dispatch`)
    # -- so it carries an engine for exactly the same reason the two above do.
    # It gets its own key because "why is this running at 15:40" has an answer,
    # and "Nightly collection" would have been the wrong one.
    _o("manual_run", label="Manual run",
       reads_provider_windows=True, has_engine=True),
    # --- The updates. ONE activation job, no window, no fraction to show. ---
    _o("mapping_change", label="Mapping change",
       reads_provider_windows=False, has_engine=True),
    _o("plan_change", label="Plan change",
       reads_provider_windows=False, has_engine=True),
    _o("first_candidate", label="First candidate",
       reads_provider_windows=False, has_engine=True),
    _o("country_activation", label="Country activation",
       reads_provider_windows=False, has_engine=True),
    # --- The three bounded verbs. Two retired, and ONE now built. ---
    #
    # `reads_provider_windows` states what the VERB is, not what it does today:
    # a synchronize and a reload would call the provider, a reprocess reapplies
    # a mapping to retained data and would not. It is `has_engine` that decides
    # whether anything is minted.
    #
    # Synchronize and Reload were RETIRED as recovery verbs on 2026-08-12 -- the
    # schedule and the day-by-day coverage of story 58.4 already deliver them --
    # so their `has_engine=False` is not a gap waiting to be filled: it is the
    # arbitration, and the refusal names the delivered gesture that replaces it.
    _o("bounded_synchronize", label="Synchronize",
       reads_provider_windows=True, has_engine=False),
    _o("bounded_reload", label="Reload",
       reads_provider_windows=True, has_engine=False),
    # BUILT 2026-08-17 (chantier 67-15b). `core.datastream_reprocess` replays the
    # retained artifact through the import path that already lands, promotes and
    # publishes (`inbound_reprocess` -> `ingest_inbound_file` -> `run_import`),
    # then appends the output version that names where the rows can be READ. The
    # execution is minted by that import path and carries this origin because
    # `ingest_inbound_file(run_origin=...)` stamps the projection plan it reuses;
    # `import_runner` advances it to a terminal state, which is the mechanism
    # `managed_feed_ledger` already declares in the minting sweep.
    _o("bounded_reprocess", label="Reprocess",
       reads_provider_windows=False, has_engine=True),
)

BY_KEY: dict[str, RunOrigin] = {origin.key: origin for origin in RUN_ORIGINS}

#: Every key the registry knows, in machine order.
ORIGIN_KEYS: tuple[str, ...] = tuple(o.key for o in RUN_ORIGINS)
#: The origins an execution may actually be minted for.
MINTABLE_ORIGINS: tuple[str, ...] = tuple(o.key for o in RUN_ORIGINS if o.has_engine)
#: The origins whose gesture refuses, because nothing would advance the run.
REFUSED_ORIGINS: tuple[str, ...] = tuple(o.key for o in RUN_ORIGINS if not o.has_engine)
#: The origins that pull windows from a provider -- the only ones with days to
#: count, and therefore the only ones a progress fraction may ever describe.
COLLECTION_ORIGINS: tuple[str, ...] = tuple(
    o.key for o in RUN_ORIGINS if o.reads_provider_windows
)

# The individual keys, so a writer stamps a constant instead of typing a string.
SCHEDULER_NIGHTLY = "scheduler_nightly"
SCHEDULER_HOURLY = "scheduler_hourly"
REFETCH = "refetch"
MAPPING_CHANGE = "mapping_change"
PLAN_CHANGE = "plan_change"
FIRST_CANDIDATE = "first_candidate"
COUNTRY_ACTIVATION = "country_activation"
BOUNDED_SYNCHRONIZE = "bounded_synchronize"
BOUNDED_RELOAD = "bounded_reload"
BOUNDED_REPROCESS = "bounded_reprocess"

#: The refusal code every path that cannot mint answers with. One code, so the
#: console and the MCP recognise the same refusal wherever it is raised.
NO_ENGINE = "no_run_engine"


def label_for(key: Any) -> str | None:
    """The English words for this origin, or None when the registry has none.

    None is NOT a label. A reader that receives it shows the raw key it was
    given, unpainted -- an origin this build has never heard of is a fact about
    the build, and folding it onto a neighbouring label would be a screen
    inventing a reason for a treatment it does not recognise.
    """
    entry = BY_KEY.get(str(key or ""))
    return entry.label if entry else None


def reads_provider_windows(key: Any) -> bool:
    """Would a run of this origin pull provider windows? Unknown answers False.

    Fail-closed towards SILENCE, not towards a claim: an unrecognised origin has
    not been shown to read windows, and a screen that assumed it did would
    explain a missing progress bar with a reason it made up.
    """
    entry = BY_KEY.get(str(key or ""))
    return bool(entry and entry.reads_provider_windows)


def has_engine(key: Any) -> bool:
    """Can anything in this build carry an execution of this origin to an end?"""
    entry = BY_KEY.get(str(key or ""))
    return bool(entry and entry.has_engine)


def refusal_message(key: Any) -> str:
    """What a person is told when the verb they asked for has no engine.

    It says the three things a refusal owes: that nothing was created, why the
    verb cannot run, and what would happen if it did anyway. It is written ONCE,
    here, so the console dialog, the admin route and the MCP tool answer the
    same sentence.
    """
    label = label_for(key) or "This recovery"
    return (
        f"{label} cannot run on this build: nothing would carry the execution it "
        "creates to an end, and that execution would hold this Datastream's "
        "publication lock -- every later publication and every following night's "
        "collection would be refused. Nothing was created."
    )


def require_origin(key: Any) -> RunOrigin:
    """The registry entry for *key*, or raise. No writer invents a literal."""
    entry = BY_KEY.get(str(key or ""))
    if entry is None:
        raise RunOriginError("unknown_origin", f"unknown run origin {key!r}")
    return entry


def stamp_origin(projection_plan: Mapping[str, Any] | None, origin: str) -> dict[str, Any]:
    """Return the projection plan a mint may carry, with its origin on it.

    THE ONE FUNCTION EVERY MINTING PATH GOES THROUGH, for two reasons. It puts
    the origin in ONE place (`projection_plan_ref.origin`) so the progress route
    reads one key rather than three vocabularies; and it refuses an origin whose
    engine does not exist, which is what stops a gesture from locking a
    Datastream forever with a run nothing will ever finish.

    Raises `RunOriginError` -- `unknown_origin` or `no_run_engine`. Both are a
    programming error at the call site, never a request's fault.
    """
    entry = require_origin(origin)
    if not entry.has_engine:
        raise RunOriginError(NO_ENGINE, refusal_message(entry.key))
    plan = dict(projection_plan or {})
    plan["origin"] = entry.key
    return plan


def origin_of(projection_plan: Mapping[str, Any] | None) -> str | None:
    """The origin a stored projection plan carries, or None when it carries none.

    None is the honest answer for every execution minted before this story: the
    column was never written, and there is no way to work out afterwards which
    path created it. A reader shows that absence, it never guesses.

    A `jsonb` column reaches a reader as a `dict` through psycopg, and as a
    `str` through the paths that select it into a text context; both are
    accepted here so a caller never has to decide which one it got, and neither
    is ever the reason an origin goes missing.
    """
    plan: Any = projection_plan
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except ValueError:
            return None
    if not isinstance(plan, Mapping):
        return None
    value = plan.get("origin")
    return value if isinstance(value, str) and value else None


def as_registry_rows() -> list[dict[str, object]]:
    """The whole table as plain data -- what the console mirror is compared to."""
    return [
        {
            "key": o.key,
            "label": o.label,
            "reads_provider_windows": o.reads_provider_windows,
            "has_engine": o.has_engine,
        }
        for o in RUN_ORIGINS
    ]
