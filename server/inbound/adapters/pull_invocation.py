"""How an activation driver binds its arguments to a Connector's ``pull``.

WHY THIS IS NOT INLINE IN THE DRIVER. Two drivers make the same call --
``connector_pull_preview`` and ``connector_pull_candidate`` -- against 39
Connectors whose signatures deliberately differ: some take ``channel_id``, some
``advertiser_id``, three take ``dry_run``. One of the two learned to filter by
signature; the other did not learn to REFUSE, and the difference cost the
product every first candidate it ever attempted.

MEASURED, 2026-08-12. ``app.datastream_activation_jobs``, kind
``candidate_materialization``: 17 jobs, 17 in ``dead_letter``, zero ever
``done``. Each died in 0.4 s, before any network call, on::

    TypeError: pull_audience_demographics() missing 2 required positional
               arguments: 'date_from' and 'date_to'

The driver dropped every ``None`` argument -- correct, so that a Connector that
never declared a keyword does not receive it -- and thereby dropped the two the
Connector REQUIRED. Dropping an optional argument and dropping a mandatory one
are opposite acts, and the filter could not tell them apart because it only ever
asked what the pull ACCEPTS, never what it DEMANDS.

WHY A NAMED REFUSAL AND NOT A REPAIR HERE. This module cannot invent a value: a
window it made up would send a Connector at a period no scheduled run will ever
cover, and the operator would review a sample of nothing real. What it can do is
stop an untyped ``TypeError`` from reaching the queue, where
``queue.py`` recorded it under a single word (``activation_work_failed``) that
named neither the argument, nor the Connector, nor the setting that fixes it.
Seventeen dead letters said the same nothing seventeen times.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Mapping

from core.datastream_activation import ActivationValidationError

__all__ = ["bind_pull_arguments"]

#: The arguments that carry the pull window. Named so the refusal can point at
#: the SETTING a person changes rather than at a Python parameter.
_WINDOW_ARGUMENTS = frozenset({"date_from", "date_to"})

#: Plumbing the CALLER mints, never a choice anyone makes on a screen. A refusal
#: naming these must not send the operator to the setup they cannot repair.
_CALLER_SUPPLIED_ARGUMENTS = frozenset({"pull_id", "project_id", "connection_id"})

_BINDABLE_KINDS = (
    inspect.Parameter.POSITIONAL_ONLY,
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
    inspect.Parameter.KEYWORD_ONLY,
)


def _refusal_sentence(missing: list[str], *, module: Any, report_id: Any, what: str) -> str:
    """One sentence naming the gesture that repairs, never the traceback."""
    connector = str(module)
    report = f" report {report_id!r}" if report_id else ""
    if set(missing) <= _WINDOW_ARGUMENTS:
        return (
            f"The {what} cannot run: Connector {connector!r}{report} requires a date window "
            f"and this Datastream resolved none. Set a Retrieval window on the Datastream's "
            f"schedule, or ask for this {what} with an explicit date range."
        )
    names = ", ".join(sorted(missing))
    if set(missing) <= _CALLER_SUPPLIED_ARGUMENTS:
        # NOT A GESTURE THE OPERATOR CAN MAKE. These are minted by whoever calls
        # the pull, never chosen on a screen, so telling someone to complete the
        # source setup sends them to repair something that was never theirs.
        return (
            f"The {what} cannot run: Connector {connector!r}{report} requires {names}, "
            f"which this deployment did not supply. Nothing on the Datastream is "
            f"missing; this is a defect in the {what} itself."
        )
    return (
        f"The {what} cannot run: Connector {connector!r}{report} requires {names}, which "
        f"this Datastream does not declare. Complete the source setup for this Connector "
        f"so the {names} it needs is on the Datastream."
    )


def bind_pull_arguments(
    pull: Callable[..., Any],
    arguments: Mapping[str, Any],
    *,
    module: Any,
    report_id: Any = None,
    what: str = "pull",
) -> dict[str, Any]:
    """The keyword arguments to call *pull* with, or a NAMED refusal.

    Two rules, and they are not the same rule:

    * **Accepts.** Only what this ``pull`` declares survives, because passing a
      keyword a module never named raises ``TypeError`` and the old code read
      that as "this Connector cannot be previewed". A ``**kwargs`` pull accepts
      everything, so nothing is filtered from it.
    * **Demands.** A parameter with no default that no argument supplies is a
      refusal, raised here with a sentence, rather than a ``TypeError`` raised
      inside the Connector with a Python signature.

    A signature that cannot be read (a C callable, a wrapper without
    ``__wrapped__``) yields no filtering and no refusal: an unreadable signature
    is not evidence of a missing argument, and inventing one would refuse pulls
    that work.
    """
    try:
        parameters = inspect.signature(pull).parameters
    except (TypeError, ValueError):
        return {key: value for key, value in arguments.items() if value is not None}

    takes_any = any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values()
    )
    supplied = {key: value for key, value in arguments.items() if value is not None}

    missing = [
        name
        for name, param in parameters.items()
        if param.default is inspect.Parameter.empty
        and param.kind in _BINDABLE_KINDS
        and name not in supplied
    ]
    if missing:
        raise ActivationValidationError(
            _refusal_sentence(missing, module=module, report_id=report_id, what=what)
        )

    if takes_any:
        return supplied
    return {key: value for key, value in supplied.items() if key in parameters}
