"""The five states a Project capability may be in, and the ONE way to read one.

WHY THIS MODULE EXISTS. `app.project_capabilities.state` is the switch every
conditional surface hangs on -- a column with a five-value CHECK (migration 131)
and, until this file, no name in Python. Every reader spelled its own literals:

  * `datastream_daily_breakdown_api` reads `ready`/`degraded` for `country`
    (`COUNTRY_ACTIVE_STATES`), which is right;
  * `governance_read_model._competitor_registry_lens` compares the state with
    `enabled`, which is NOT one of the five -- so that lens can never be
    available to anybody. Measured 2026-08-07 against migration 131's CHECK:
    `('disabled', 'draft', 'ready', 'degraded', 'blocked')`.

A capability is a class, not one screen: `tax_fees` opens the `Cost` tab today
and `placement_mapping` will open `Placements` tomorrow, so the vocabulary and
the read are declared once here rather than a third time in each.

WHY `ready` AND `degraded` BOTH COUNT AS ACTIVE. `reports.py` already gates a
projection on exactly these two and `country` follows it: `degraded` shows the
capability AND says it is degraded, because hiding evidence already collected is
worse than showing it diminished.

WHAT THIS MODULE IS NOT. It is not an activation authority. Nothing here writes,
and a capability's state is decided by a Project Change Set in Project Settings >
Capabilities and nowhere else. Reading it here can never turn one on.
"""

from __future__ import annotations

#: Migration 131's CHECK, mirrored exactly. Ordered from off to on so a reader
#: can see the machine; nothing depends on the order.
CAPABILITY_STATES: tuple[str, ...] = ("disabled", "draft", "ready", "degraded", "blocked")

#: The SEVEN keys the database accepts, in the order the CHECKs spell them:
#: migration 131 for the first five, migration 243 for `placement_mapping`
#: (story 61.5), migration 309 for `analytics_alignment` (story 70.3). The order
#: matters beyond readability -- `read_project_settings` ranks its rows the same
#: way and `validate_capability_ledger` compares the result to
#: `CAPABILITY_SPECS`.
PROJECT_CAPABILITY_KEYS: tuple[str, ...] = (
    "country",
    "currency_fx",
    "reporting_timezone",
    "tax_fees",
    "competitors",
    "placement_mapping",
    "analytics_alignment",
)

#: Which capabilities the database REFUSES to turn off, mirrored from the same
#: migrations: `CHECK (NOT (availability='always_present' AND state='disabled'))`
#: plus the key/availability pairing CHECK, which migration 243 re-added with
#: `placement_mapping` inside its `optional` member and migration 309 re-added
#: again with `analytics_alignment` beside it. A capability declared
#: `always_present` carries no switch on any surface — the control is what is
#: forbidden, not the row.
CAPABILITY_AVAILABILITY: dict[str, str] = {
    "country": "optional",
    "currency_fx": "always_present",
    "reporting_timezone": "always_present",
    "tax_fees": "optional",
    "competitors": "optional",
    "placement_mapping": "optional",
    "analytics_alignment": "optional",
}

#: The states that make a capability's evidence readable.
CAPABILITY_ACTIVE_STATES: tuple[str, ...] = ("ready", "degraded")

#: No row in `app.project_capabilities` at all. Distinct from `disabled`, which is
#: a decision somebody took; this is a Project the control plane never wrote.
#: `app.seed_project_capabilities` writes one row per declared capability --
#: seven since migration 309 -- so a missing row means the seed never ran, which
#: is a fact, not a default to paper over.
CAPABILITY_STATE_UNSET = "unset"

#: `tax_fees` opens the Datastream `Cost` tab (amendment « Une capacité activée
#: AJOUTE son onglet », `datastream-workbench-and-wizard.md`).
TAX_FEES_CAPABILITY_KEY = "tax_fees"

#: `placement_mapping` opens the Datastream `Placements` tab -- the second entry
#: the same amendment names, delivered by story 61.1. Declared beside its sibling
#: rather than spelled as a literal in the read model: a key written twice is a
#: key that can be misspelled once and read `unset` forever.
PLACEMENT_MAPPING_CAPABILITY_KEY = "placement_mapping"

#: `analytics_alignment` opens NO tab -- story 70.3. It adds three columns to an
#: aggregation (`core/analytics_alignment.py`, `ADDED_COLUMNS`), which is the
#: other kind of effect the same amendment describes, and declaring a tab it does
#: not open would put an empty band on every Workbench.
ANALYTICS_ALIGNMENT_CAPABILITY_KEY = "analytics_alignment"

#: The switch, read from the control plane and from nowhere else.
_CAPABILITY_STATE_SQL = """
    SELECT state
    FROM app.project_capabilities
    WHERE project_id = %s AND capability_key = %s
"""


def read_capability_state(conn, *, project_id: str, capability_key: str) -> str:
    """One of `CAPABILITY_STATES`, or `unset` -- in ONE statement.

    ONE STATEMENT, on the branch every call takes: the state is what decides
    whether an expensive read happens at all, so it cannot be read after it.
    """
    with conn.cursor() as cur:
        cur.execute(_CAPABILITY_STATE_SQL, (project_id, capability_key))
        row = cur.fetchone()
    if row is None or not row[0]:
        return CAPABILITY_STATE_UNSET
    return str(row[0])


#: The same switch, for SEVERAL keys, in one statement.
_CAPABILITY_STATES_SQL = """
    SELECT capability_key, state, availability
    FROM app.project_capabilities
    WHERE project_id = %s AND capability_key = ANY(%s)
"""


def read_capability_states(
    conn, *, project_id: str, capability_keys: tuple[str, ...] | list[str]
) -> dict[str, dict[str, str]]:
    """Every asked-for capability's state and availability -- in ONE statement.

    WHY A PLURAL READER EXISTS BESIDE THE SINGULAR ONE. The Workbench header
    carries the state of EVERY declared capability and EVERY tab loads that
    header, so calling `read_capability_state` once per key would put one round
    trip per capability on the hot path of the surface a person opens most --
    and that price grows with `PROJECT_CAPABILITY_KEYS`. The singular function
    stays exactly as it was for its own callers -- `datastream_workbench_cost`
    asks about one key and one key only, and widening it would have made that
    read pay for answers it discards.

    Every asked-for key comes back, present in the table or not: a key with no
    row is `unset` with its DECLARED availability, because the pairing CHECK of
    migrations 131 and 243 holds the two together and the database can hold
    nothing else. An absent key would
    force each caller to invent what a missing row means.
    """
    keys = list(capability_keys)
    rows: dict[str, dict[str, str]] = {}
    if keys:
        with conn.cursor() as cur:
            cur.execute(_CAPABILITY_STATES_SQL, (project_id, keys))
            for capability_key, state, availability in cur.fetchall():
                rows[str(capability_key)] = {
                    "state": str(state) if state else CAPABILITY_STATE_UNSET,
                    "availability": str(availability)
                    if availability
                    else CAPABILITY_AVAILABILITY.get(str(capability_key), "optional"),
                }
    return {
        key: rows.get(
            key,
            {
                "state": CAPABILITY_STATE_UNSET,
                "availability": CAPABILITY_AVAILABILITY.get(key, "optional"),
            },
        )
        for key in keys
    }


def capability_is_active(state: str) -> bool:
    """True for the two states that make the capability's evidence readable."""
    return state in CAPABILITY_ACTIVE_STATES
