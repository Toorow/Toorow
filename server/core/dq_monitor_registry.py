"""The ONE place that knows which Data Quality monitors exist -- story 59.5.

WHY THIS MODULE EXISTS. Before it, eleven places named the monitors and none of
them agreed. Measured on 2026-08-08:

  * `dq_monitors.CHECK_PROFILES`      -- 7 keys (what the nightly sweep dispatches)
  * `controls_quality.DQ_CHECKS`      -- 8 keys (+ `geography`, publishable, never dispatched)
  * `dq_api._DQ_TYPES`                -- 6 types (`dq_null_rate` and `dq_zero_rows` ABSENT)
  * `dq_api._MONITOR_LABELS`          -- 6 labels, in French
  * the types actually written        -- 9 firing sites, 8 distinct types
  * `test_infra_alerts.py`'s expected -- 7 types, without the `dq_geography` it reads
  * `doc/datastream/04`               -- 5 monitors
  * the console                       -- 2 f-strings

Nine `^def _check_` functions, seven dispatched, eight publishable, eight alert
types. No single number is right on its own, which is exactly why a name alone
could never carry the vocabulary: FOUR attributes separate those lists.

  * `geography` is publishable and displayable but NOT dispatched, because it is
    scoped to a PROJECT and the sweep walks Datastreams. Without `target_kind` it
    is indistinguishable from its eight siblings -- and `dq_governance.TARGET_KINDS`
    has no `project` value, so `ensure_monitor` cannot create one. That refusal is
    deliberate (story 59.5, arbitrage 4): naming the scope costs nothing, and a
    migration on the `145:306-307` CHECK for a monitor the plan only asks to NAME
    would be a schema change bought with nothing.
  * `arrival_timeliness` fires under ANOTHER entry's `alert_type`. It answers the
    same operator question as `timeliness` ("is my data late?") for a feed that is
    delivered rather than pulled, so it writes `dq_timeliness` and marks itself
    with `firing_kind="arrival"` in the metadata (`dq_monitors.py:793`). Without
    `key` and `alert_type` being separate fields, it is either a tenth type nobody
    reads or invisible.

WHAT DERIVES FROM HERE, and therefore cannot drift again: `CHECK_PROFILES`'s keys,
`controls_quality.DQ_CHECKS`, `dq_api._DQ_TYPES`, `dq_api._MONITOR_LABELS`, the
`app.dq_monitors.label` written by `dq_null_rate` / `dq_zero_rows` / the story 59.5
bridges, and the expected list of the firing-message guard
(`tests/test_infra_alerts.py`). `tests/conformance/test_dq_monitor_registry.py`
refuses a monitor any of those layers does not know about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# The two scopes a monitor can watch.
# ---------------------------------------------------------------------------
#
# `TARGET_DATASTREAM` is the only one `app.dq_monitors.target_kind` accepts among
# the two (migration 145:306-307 allows `datastream`, `output`, `semantic_concept`
# and `semantic_view`). `TARGET_PROJECT` is therefore a statement about WHAT IS
# WATCHED, not a value that can be stored -- and that is the whole reason
# `geography` cannot be bridged into `app.dq_evaluations` today.

TARGET_DATASTREAM = "datastream"
TARGET_PROJECT = "project"

#: What the `monitor_kind` metadata of a firing may say. One value exists, and it
#: was orphaned until this registry: `dq_monitors.py:793`.
FIRING_KIND_ARRIVAL = "arrival"

#: `app.dq_monitors.name` CHECK, migration 145:299, and `label` is bounded to 160.
_NAME_SHAPE = re.compile(r"^[a-z][a-z0-9_]{0,126}$")
_LABEL_MAX = 160


@dataclass(frozen=True)
class DqMonitor:
    """One monitor, and every question a reader is allowed to ask about it."""

    #: The check profile key. What `CHECK_PROFILES`, `DQ_CHECKS` and
    #: `dq_monitor_versions.check_profile` all name.
    key: str
    #: The `app.alert_firings.type` this check writes. NOT one-to-one with `key`:
    #: `arrival_timeliness` writes `dq_timeliness`.
    alert_type: str
    #: What a person reads. English, always -- it reaches `app.dq_monitors.label`,
    #: the DQ summary and the run's anomaly panel.
    label: str
    #: `datastream` or `project`. Only the first can be stored as a target.
    target_kind: str
    #: Is it in the nightly sweep's dispatch table (`CHECK_PROFILES`)?
    dispatched: bool
    #: May a governed DQ policy publish it (`controls_quality.DQ_CHECKS`)?
    publishable: bool
    #: Does an anomaly of this monitor point at ROWS a person could be shown?
    #: A fact about the monitor's NATURE, not about this build: a window that
    #: returned nothing and an arrival that missed its deadline have no faulty
    #: row to replay, while a null rate and a rejected date do. `dq_issue_rows`
    #: reads this to decide between "there is nothing to show" and "this build
    #: cannot show it yet" -- two sentences that must never be merged, because
    #: the first is a claim about the client's data and the second about ours.
    has_faulty_rows: bool = True
    #: The `monitor_kind` this check stamps on its firing, when it shares another
    #: entry's `alert_type` and would otherwise be unattributable.
    firing_kind: str | None = None


def _m(key: str, **kwargs) -> DqMonitor:
    return DqMonitor(key=key, **kwargs)


#: The monitors, in the order their `_check_` functions are defined in
#: `dq_monitors.py`. No count is written here on purpose: it moved on 2026-08-31
#: when `unresolved_values` landed, and a number in a comment is the twelfth
#: divergent list. EVERY ONE OF THEM WRITES A FIRING -- `geography`'s call site
#: lives in `geographic_conformance.py` and `unresolved_values`'s in
#: `unresolved_values_monitor.py` -- so the firing-message guard's expected list
#: is `FIRING_ALERT_TYPES` and needs no second declaration.
DQ_MONITORS: tuple[DqMonitor, ...] = (
    _m(
        "volume",
        alert_type="dq_volume",
        label="Volume",
        target_kind=TARGET_DATASTREAM,
        dispatched=True,
        # The finding is the DAY'S TOTAL against its rolling median, not any
        # single row of it.
        has_faulty_rows=False,
        publishable=True,
    ),
    _m(
        "arrival_timeliness",
        alert_type="dq_timeliness",
        label="Arrival timeliness",
        target_kind=TARGET_DATASTREAM,
        # Reached THROUGH `timeliness`, which hands a delivered feed over to it
        # (`dq_monitors.py:938-946`): it is not a row of the dispatch table, and a
        # governed policy cannot publish it on its own.
        dispatched=False,
        # A delivery that missed its deadline is a fact about a TIME.
        has_faulty_rows=False,
        publishable=False,
        firing_kind=FIRING_KIND_ARRIVAL,
    ),
    _m(
        "timeliness",
        alert_type="dq_timeliness",
        label="Timeliness",
        target_kind=TARGET_DATASTREAM,
        dispatched=True,
        # Same nature as `arrival_timeliness`: a lateness, not a row.
        has_faulty_rows=False,
        publishable=True,
    ),
    _m(
        "duplication",
        alert_type="dq_duplication",
        label="Duplication",
        target_kind=TARGET_DATASTREAM,
        dispatched=True,
        publishable=True,
    ),
    _m(
        "schema",
        alert_type="dq_schema",
        label="Schema drift",
        target_kind=TARGET_DATASTREAM,
        dispatched=True,
        # The finding is the COLUMN SET that drifted from the baseline.
        has_faulty_rows=False,
        publishable=True,
    ),
    _m(
        "date_format",
        # The honest label the French one already carried: this check counts the
        # rows the extractor rejected, not a formatting preference.
        alert_type="dq_date_format",
        label="Rejected rows",
        target_kind=TARGET_DATASTREAM,
        dispatched=True,
        publishable=True,
    ),
    _m(
        "null_rate",
        alert_type="dq_null_rate",
        label="Null rate",
        target_kind=TARGET_DATASTREAM,
        dispatched=True,
        publishable=True,
    ),
    _m(
        "zero_rows",
        alert_type="dq_zero_rows",
        label="Zero rows",
        target_kind=TARGET_DATASTREAM,
        dispatched=True,
        # The anomaly IS the absence: a window that returned nothing has no
        # faulty row by construction. Story 59.1, arbitrage 8.
        has_faulty_rows=False,
        publishable=True,
    ),
    _m(
        "geography",
        alert_type="dq_geography",
        label="Unresolved geography",
        # PROJECT-scoped: the country partition is a project aggregate, so this
        # check runs outside the per-Datastream loop and no `app.dq_monitors` row
        # can hold it.
        target_kind=TARGET_PROJECT,
        dispatched=False,
        publishable=True,
    ),
    _m(
        "unresolved_values",
        alert_type="dq_unresolved_values",
        label="Unresolved values",
        # DATASTREAM-scoped, and that is the whole difference from `geography`
        # standing above it. The country partition is a project aggregate, so that
        # check runs outside the per-Datastream loop and can never be dispatched.
        # This one evaluates one Datastream and one of ITS mapped dimensions at a
        # time (`unresolved-values.md`: "Datastream-scoped, so it rides the nightly
        # sweep. This is where it differs from the geography monitor"), so it is in
        # the dispatch table and a governed policy can publish it.
        target_kind=TARGET_DATASTREAM,
        dispatched=True,
        # The finding is a set of DISTINCT VALUES a reading could not name -- a
        # fact about a vocabulary, the way `schema`'s is about a column set. The
        # values themselves are replayed live by the S1 panel
        # (`unresolved_values.read_unresolved_set`, and the firing deliberately
        # carries only the top few by cost), never by the faulty-row replay, which
        # answers about the rows of one window.
        has_faulty_rows=False,
        publishable=True,
    ),
)

BY_KEY: dict[str, DqMonitor] = {monitor.key: monitor for monitor in DQ_MONITORS}


def _distinct(values) -> tuple[str, ...]:
    """Order of first appearance, without duplicates -- `dict` keeps the order."""
    return tuple(dict.fromkeys(values))


#: The keys the nightly sweep dispatches. `dq_monitors.CHECK_PROFILES` IS this
#: tuple, mapped to its adapters.
DISPATCHED_KEYS: tuple[str, ...] = tuple(m.key for m in DQ_MONITORS if m.dispatched)

#: The keys a published DQ policy may name. `controls_quality.DQ_CHECKS` IS this.
PUBLISHABLE_KEYS: tuple[str, ...] = tuple(m.key for m in DQ_MONITORS if m.publishable)

#: The firing types a DQ reader must be able to display. `dq_api._DQ_TYPES` IS
#: this -- and it held SIX until this registry, so `dq_null_rate` and
#: `dq_zero_rows` were filtered OUT of every DQ summary the five live callers of
#: `fetch_dq_report_data` read.
PUBLISHABLE_ALERT_TYPES: tuple[str, ...] = _distinct(
    m.alert_type for m in DQ_MONITORS if m.publishable
)

#: Every type written to `app.alert_firings` by a DQ check, including the one that
#: only an unpublishable entry writes. What the firing-message guard expects.
FIRING_ALERT_TYPES: tuple[str, ...] = _distinct(m.alert_type for m in DQ_MONITORS)

#: What a person reads for a firing type. `dq_api._MONITOR_LABELS` IS this.
#: Keyed on the PUBLISHABLE entries: `arrival_timeliness` shares `dq_timeliness`,
#: and a display list holding two rows for one type would double the summary.
LABELS_BY_ALERT_TYPE: dict[str, str] = {
    m.alert_type: m.label for m in DQ_MONITORS if m.publishable
}

#: The keys whose target can actually be stored in `app.dq_monitors.target_kind`.
#: Everything else is named here and governed nowhere -- deliberately, and said.
DATASTREAM_SCOPED_KEYS: tuple[str, ...] = tuple(
    m.key for m in DQ_MONITORS if m.target_kind == TARGET_DATASTREAM
)


def label_for(key: str) -> str:
    """The English label of a check profile, or the key itself.

    The key is a poor label and an honest one: a check this build does not know
    is named by what it is called, never by an invented sentence.
    """
    entry = BY_KEY.get(str(key or ""))
    return entry.label if entry else str(key or "")


def label_for_alert_type(alert_type: str) -> str:
    """The English label of a firing type, or the type itself."""
    return LABELS_BY_ALERT_TYPE.get(str(alert_type or ""), str(alert_type or ""))


def published_profile(key: str) -> str | None:
    """The `check_profile` a governed version of this monitor may carry.

    A published version is validated against `controls_quality.DQ_CHECKS`
    (`publish_version` -> `_validate_dq_payload`), so a monitor that is NOT
    publishable cannot carry its own key there. It carries the key of the sibling
    whose `alert_type` it fires under -- `arrival_timeliness` is governed as
    `timeliness`, which is exactly what it is: the same operator question, asked of
    a feed that is delivered rather than pulled, and the governed evaluator
    dispatching `timeliness` on a delivered Datastream reaches the arrival branch
    anyway.

    The monitor stays distinguishable by its NAME and its LABEL, which are
    `arrival_timeliness`'s, and by the `monitor_kind` its firing carries.
    """
    entry = BY_KEY.get(str(key or ""))
    if entry is None:
        return None
    if entry.publishable:
        return entry.key
    sibling = next(
        (m for m in DQ_MONITORS if m.publishable and m.alert_type == entry.alert_type),
        None,
    )
    return sibling.key if sibling else None


def monitor_name(key: str, datastream_id: str) -> str | None:
    """`<key>_<datastream>` folded to the name CHECK of migration 145:299.

    The same shape `dq_null_rate` and `dq_zero_rows` mint by hand, written once so
    a bridge added later cannot invent a third naming scheme.
    """
    if key not in BY_KEY:
        return None
    slug = re.sub(r"[^a-z0-9_]", "_", str(datastream_id or "").strip().lower())
    if not slug:
        # A monitor named after nothing would be one monitor for every anonymous
        # caller of this function, all colliding on `(project_id, name)`.
        return None
    name = f"{key}_{slug}"[:127]
    return name if _NAME_SHAPE.match(name) else None


def instance_label(key: str, datastream_name: str) -> str:
    """`<Label>: <Datastream>` -- what `app.dq_monitors.label` holds.

    It is the text the run's anomaly panel renders (`RunAnomalies.tsx:237`, through
    `dq_issue_rows.py:324`), so it is the ONE place a monitor's name reaches a
    person today. Bounded to the 160 characters the column accepts.
    """
    return f"{label_for(key)}: {datastream_name}"[:_LABEL_MAX]


def as_registry_rows() -> list[dict[str, object]]:
    """The whole table as plain data -- what a mirror is compared to."""
    return [
        {
            "key": m.key,
            "alert_type": m.alert_type,
            "label": m.label,
            "target_kind": m.target_kind,
            "dispatched": m.dispatched,
            "publishable": m.publishable,
            "firing_kind": m.firing_kind,
        }
        for m in DQ_MONITORS
    ]
