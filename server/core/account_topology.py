"""toorow -- Account topology & onboarding scoping (Story 25.5).

The generic, provider-agnostic core of the onboarding flow declared in the
playbook (server/modules/README.md, step 5):

    discovery -> selection -> access check -> bounded trial -> windowed backfill

A pull can NEVER run against an unselected/unverified reporting account, and no
connector may implicitly vacuum a whole account: account selection, access
verification, the bounded trial extraction and the windowed backfill are ALL
core-owned flows. The connector supplies only a manifest declaration
(``account_topology``) and a discovery callable that lists what the token can
reach; core owns the state machine and the scope storage.

AD-2 (ZERO provider vocabulary): this module contains NO provider names and NO
provider-specific account concepts. ``levels`` / ``account_id`` / ``account_label``
are opaque strings resolved by the connector's discovery call; core stores and
compares them but never interprets them. The selection_level id and the
discovery callable name both live in the module's manifest, never here.

Exports:
  * get_topology(manifest) -> dict | None      -- validated topology contract or None
  * validate_topology(topology) -> list[str]   -- contract errors ([] when valid)
  * discover_accounts(...)                      -- run the module discovery callable
  * verify_and_select_account(...)              -- access-check + persist ready scope
  * resolve_selected_account(connection_ref_id) -- pull-site account resolution hook
  * get_scope(connection_ref_id) -> dict | None -- read the current scope row
  * has_ready_scope(connection_ref_id) -> bool  -- enqueue-guard probe (AC4)
  * enqueue_trial_pull(...)                      -- bounded (3-day) trial via enqueue_pull
  * compute_backfill_windows(days) -> list[dict] -- 1..365 -> <=31-day windows

Env-var account fallbacks (``*_ACCOUNT_ID``) are DEPRECATED and removed per-module
at rollout (the meta-ads stitch + Story 25.7): once a module declares
``account_topology``, ``resolve_selected_account`` is the single source of the
reporting account -- an env-var default is a scoping bypass and must not survive.

Windows/CI note (AI-03): all log/message strings use ASCII-safe characters only.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (named so they are configurable later, per Dev Notes).
# ---------------------------------------------------------------------------

# The manifest key that opts a module into the topology onboarding flow.
ACCOUNT_TOPOLOGY_KEY = "account_topology"

# Scope state machine (mirrors the CHECK constraint in migration 046).
STATE_PENDING = "pending_account_selection"
STATE_READY = "ready"

# The bounded TRIAL pull window: the last N days ending yesterday. Named so a
# later story can make it configurable; today it is a constant (Dev Notes).
TRIAL_PULL_DAYS = 3

# Backfill validation + windowing bounds (AC3).
BACKFILL_MIN_DAYS = 1
BACKFILL_MAX_DAYS = 365
BACKFILL_WINDOW_DAYS = 31


# ---------------------------------------------------------------------------
# ULID helper
# ---------------------------------------------------------------------------


def _mint_scope_id() -> str:
    """Mint a new ULID with 'ascope_' prefix."""
    from ulid import ULID  # noqa: PLC0415

    return f"ascope_{ULID()}"


# ---------------------------------------------------------------------------
# AC1 -- topology contract validation + reader (AD-2: no provider vocabulary).
# ---------------------------------------------------------------------------


def validate_topology(topology) -> list[str]:
    """Return a list of contract-violation strings for *topology* ([] when valid).

    The contract (AC1):
        {
          "levels": [{"id": str, "label": str}, ...],   # >= 1, unique ids
          "selection_level": "<one of levels[].id>",
          "discovery": {"callable": "<connector fn name>"}
        }

    Generic only: ids/labels are opaque. Returns human-readable ASCII errors so a
    conformance test or a startup check can surface exactly what is malformed.
    """
    errors: list[str] = []
    if not isinstance(topology, dict):
        return ["account_topology must be an object"]

    levels = topology.get("levels")
    if not isinstance(levels, list) or not levels:
        errors.append("account_topology.levels must be a non-empty array")
        levels = []

    level_ids: list[str] = []
    for idx, level in enumerate(levels):
        if not isinstance(level, dict):
            errors.append(f"account_topology.levels[{idx}] must be an object")
            continue
        lid = level.get("id")
        label = level.get("label")
        if not isinstance(lid, str) or not lid:
            errors.append(f"account_topology.levels[{idx}].id must be a non-empty string")
        else:
            level_ids.append(lid)
        if not isinstance(label, str) or not label:
            errors.append(f"account_topology.levels[{idx}].label must be a non-empty string")

    if len(level_ids) != len(set(level_ids)):
        errors.append("account_topology.levels[].id values must be unique")

    selection_level = topology.get("selection_level")
    if not isinstance(selection_level, str) or not selection_level:
        errors.append("account_topology.selection_level must be a non-empty string")
    elif level_ids and selection_level not in level_ids:
        errors.append(
            "account_topology.selection_level must reference a declared level id"
        )

    discovery = topology.get("discovery")
    if not isinstance(discovery, dict):
        errors.append("account_topology.discovery must be an object")
    else:
        callable_name = discovery.get("callable")
        if not isinstance(callable_name, str) or not callable_name:
            errors.append(
            "account_topology.discovery.callable must be a non-empty string"
        )

    return errors


def get_topology(manifest) -> dict | None:
    """Return the validated ``account_topology`` dict from *manifest*, or None.

    None means the module does NOT participate in the topology flow (backward
    compatible: the enqueue guard and endpoints treat these modules unchanged).
    A PRESENT-but-INVALID declaration returns None and logs a warning -- a
    malformed contract must not silently behave like a valid one, but it also
    must not crash the loader (AC1 is a generic reader, not a hard gate).
    """
    if not isinstance(manifest, dict):
        return None
    topology = manifest.get(ACCOUNT_TOPOLOGY_KEY)
    if topology is None:
        return None
    errors = validate_topology(topology)
    if errors:
        logger.warning(
            "account_topology: invalid contract in manifest name=%s errors=%s",
            manifest.get("name", "?"),
            "; ".join(errors),
        )
        return None
    return topology


# ---------------------------------------------------------------------------
# Module registry access (AD-2-safe: core -> core.main accessor only).
# ---------------------------------------------------------------------------


def _resolve_module(provider: str):
    """Return the loaded module object for *provider*, or None.

    Uses the core.main public accessor (never imports server/modules/* -- AD-2).
    """
    try:
        from core.main import get_loaded_modules  # noqa: PLC0415

        for loaded in get_loaded_modules():
            if loaded.name == provider:
                return loaded
    except Exception as exc:  # noqa: BLE001
        logger.warning("account_topology: module registry unavailable: %s", exc)
    return None


def get_topology_for_provider(provider: str) -> dict | None:
    """Return the validated topology dict for a loaded *provider*, or None."""
    loaded = _resolve_module(provider)
    if loaded is None:
        return None
    return get_topology(getattr(loaded, "manifest", None))


def _resolve_discovery_callable(provider: str):
    """Return the connector's discovery callable for *provider*, or None.

    Reads the callable NAME from the manifest's account_topology.discovery and
    getattr's it off the connector module (AD-2: the name lives in the manifest,
    never hard-coded here). Returns None when the module/topology/callable is
    absent or not callable (fail closed).
    """
    loaded = _resolve_module(provider)
    if loaded is None:
        return None
    topology = get_topology(getattr(loaded, "manifest", None))
    if topology is None:
        return None
    callable_name = topology.get("discovery", {}).get("callable")
    if not isinstance(callable_name, str) or not callable_name:
        return None
    fn = getattr(loaded.connector_module, callable_name, None)
    return fn if callable(fn) else None


# ---------------------------------------------------------------------------
# Connection resolution (provider + nango_connection_id + project_id).
# ---------------------------------------------------------------------------


def resolve_connection_ref(connection_ref_id: str) -> dict | None:
    """Fetch {id, nango_connection_id, provider, project_id} for a connection_ref.

    Returns None when the row does not exist. Mirrors queue._resolve_connection_ref
    but is self-contained (endpoints call this before running discovery).
    """
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, nango_connection_id, provider, project_id
                FROM app.connection_ref
                WHERE id = %s
                """,
                (connection_ref_id,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "nango_connection_id": row[1],
        "provider": row[2],
        "project_id": row[3],
    }


# ---------------------------------------------------------------------------
# AC3 (discovery) -- run the module discovery callable for a connection.
# ---------------------------------------------------------------------------


def discover_accounts(connection_ref_id: str) -> dict:
    """Run the module's discovery callable and return the reachable accounts.

    Returns a dict:
        {"topology": <levels/selection_level>, "accounts": [...]}

    Raises:
        NoTopologyError   -- the module declares no (valid) account_topology.
        ConnectionNotFound-- the connection_ref does not exist.
    Any typed connector error (core.pull_errors.ConnectorError) or RateLimitError
    from the discovery call propagates unchanged so the endpoint can map it.
    """
    ref = resolve_connection_ref(connection_ref_id)
    if ref is None:
        raise ConnectionNotFound(connection_ref_id)

    provider = ref["provider"]
    topology = get_topology_for_provider(provider)
    if topology is None:
        raise NoTopologyError(provider)

    discovery_fn = _resolve_discovery_callable(provider)
    if discovery_fn is None:
        # Contract declared but the connector does not implement the callable:
        # this is a module bug, surfaced clearly rather than as a generic 500.
        raise NoTopologyError(provider)

    # The connector receives the identifier it always receives from the pull path
    # (the nango_connection_id) and resolves its own token via get_fresh_token.
    accounts = discovery_fn(ref["nango_connection_id"])
    from core.project_access import epic36_production_access_enabled  # noqa: PLC0415

    if epic36_production_access_enabled():
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            reconcile_discovered_accounts(connection_ref_id, accounts, conn)
            conn.commit()
    return {"topology": topology, "accounts": accounts}


def reconcile_discovered_accounts(connection_ref_id: str, accounts, conn) -> None:
    """Replace the available account set and invalidate stale exposure atomically."""
    account_ids = _flatten_account_ids(accounts)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.credential_accounts SET available = FALSE WHERE credential_id = %s",
            (connection_ref_id,),
        )
        for account_id in sorted(account_ids):
            cur.execute(
                "INSERT INTO app.credential_accounts "
                "(credential_id, external_account_id, available, "
                "discovery_generation, last_seen_at) "
                "VALUES (%s, %s, TRUE, 1, NOW()) "
                "ON CONFLICT (credential_id, external_account_id) DO UPDATE SET "
                "available = TRUE, discovery_generation = "
                "app.credential_accounts.discovery_generation + 1, last_seen_at = NOW()",
                (connection_ref_id, account_id),
            )
        cur.execute(
            "UPDATE app.credential_account_grants g SET status = 'invalidated', "
            "invalidated_at = NOW(), invalidation_reason = 'rediscovery' "
            "FROM app.credential_accounts a WHERE a.credential_id = %s "
            "AND a.credential_id = g.credential_id "
            "AND a.external_account_id = g.external_account_id "
            "AND a.available = FALSE AND g.status = 'active'",
            (connection_ref_id,),
        )
        cur.execute(
            "UPDATE app.connection_account_scope s "
            "SET state = 'pending_account_selection', verified_at = NULL, updated_at = NOW() "
            "WHERE s.connection_ref_id = %s AND NOT EXISTS ("
            "SELECT 1 FROM app.credential_accounts a "
            "WHERE a.credential_id = s.connection_ref_id "
            "AND a.external_account_id = s.account_id AND a.available = TRUE)",
            (connection_ref_id,),
        )


def invalidate_credential_exposures(connection_ref_id: str, reason: str, conn) -> None:
    """Invalidate every exposure and selected account for a revoked/changed credential."""
    if reason not in {"revocation", "ownership_change", "policy_change"}:
        raise ValueError("unsupported exposure invalidation reason")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.credential_account_grants SET status = 'invalidated', "
            "invalidated_at = NOW(), invalidation_reason = %s "
            "WHERE credential_id = %s AND status = 'active'",
            (reason, connection_ref_id),
        )
        cur.execute(
            "UPDATE app.connection_account_scope SET state = 'pending_account_selection', "
            "verified_at = NULL, updated_at = NOW() WHERE connection_ref_id = %s",
            (connection_ref_id,),
        )


# ---------------------------------------------------------------------------
# AC2 -- scope persistence (SQL against app.connection_account_scope).
# ---------------------------------------------------------------------------


def _row_to_scope(cols, row) -> dict:
    record: dict = {}
    _TS_COLS = {"verified_at", "created_at", "updated_at"}
    for col, val in zip(cols, row):
        if col in _TS_COLS and val is not None:
            record[col] = val.isoformat()
        else:
            record[col] = val
    return record


def get_scope(connection_ref_id: str) -> dict | None:
    """Return the current scope row for a connection as a dict, or None."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, connection_ref_id, account_id, account_label, state,
                       verified_at, selected_by, created_at, updated_at
                FROM app.connection_account_scope
                WHERE connection_ref_id = %s
                """,
                (connection_ref_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [desc[0] for desc in cur.description]
    return _row_to_scope(cols, row)


def has_ready_scope(connection_ref_id: str, conn=None) -> bool:
    """True when a ``state='ready'`` scope row exists for the connection (AC4).

    Accepts an OPTIONAL open connection so callers already holding one (the
    enqueue path) do not open a second. When *conn* is None a connection is
    opened here. Any DB failure returns False -- fail closed: an unverifiable
    scope must never be treated as ready.
    """

    def _probe(_conn) -> bool:
        with _conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM app.connection_account_scope
                WHERE connection_ref_id = %s AND state = %s
                LIMIT 1
                """,
                (connection_ref_id, STATE_READY),
            )
            return cur.fetchone() is not None

    try:
        if conn is not None:
            return _probe(conn)
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as own_conn:
            return _probe(own_conn)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "account_topology: has_ready_scope probe failed conn=%s: %s",
            connection_ref_id,
            exc,
        )
        return False


def upsert_scope(
    connection_ref_id: str,
    *,
    account_id: str | None,
    account_label: str | None,
    state: str,
    selected_by: str,
    verified_at: datetime | None,
) -> dict:
    """UPSERT the single living scope row for a connection and return it.

    One row per connection (unique index): re-selecting an account UPSERTs the
    same row rather than accumulating history (the trail lives in audit_log).
    """
    from core.db import get_connection  # noqa: PLC0415

    scope_id = _mint_scope_id()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.connection_account_scope
                    (id, connection_ref_id, account_id, account_label, state,
                     verified_at, selected_by, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now())
                ON CONFLICT (connection_ref_id) DO UPDATE
                    SET account_id    = EXCLUDED.account_id,
                        account_label = EXCLUDED.account_label,
                        state         = EXCLUDED.state,
                        verified_at   = EXCLUDED.verified_at,
                        selected_by   = EXCLUDED.selected_by,
                        updated_at    = now()
                RETURNING id, connection_ref_id, account_id, account_label, state,
                          verified_at, selected_by, created_at, updated_at
                """,
                (
                    scope_id,
                    connection_ref_id,
                    account_id,
                    account_label,
                    state,
                    verified_at,
                    selected_by,
                ),
            )
            row = cur.fetchone()
            cols = [desc[0] for desc in cur.description]
        conn.commit()
    return _row_to_scope(cols, row)


def resolve_selected_account(connection_ref_id: str) -> str | None:
    """Return the opaque selected account_id for a connection, or None (AC6).

    The generic pull-site hook: connectors/dispatch call this to read the account
    the onboarding flow selected + verified, instead of an ``*_ACCOUNT_ID`` env
    var. Only a ``state='ready'`` scope resolves an account -- a pending/absent
    scope returns None so nothing runs against an unverified account. Env-var
    account fallbacks are DEPRECATED and removed per-module at rollout (meta-ads
    stitch + Story 25.7).
    """
    scope = get_scope(connection_ref_id)
    if scope is None:
        return None
    if scope.get("state") != STATE_READY:
        return None
    return scope.get("account_id")


# ---------------------------------------------------------------------------
# AC3 -- access-check + persist ready scope (the selection endpoint's core).
# ---------------------------------------------------------------------------


def verify_and_select_account(
    connection_ref_id: str,
    account_id: str,
    *,
    selected_by: str,
) -> dict:
    """Verify access to *account_id*, then persist a ``ready`` scope row.

    Access verification is a MINIMAL read against THAT account via the module's
    discovery path: we re-run discovery and confirm the requested account is in
    the reachable set. This proves the token can actually reach the selected
    account (no blind trust of a client-supplied id) without inventing a
    provider-specific access-check call (AD-2). On success the scope is UPSERTed
    to ``state='ready'`` with ``verified_at`` set to now.

    Returns the persisted scope dict.

    Raises:
        NoTopologyError    -- the module declares no topology.
        ConnectionNotFound -- the connection_ref does not exist.
        AccountNotReachable-- *account_id* is not in the reachable set.
    Typed connector errors from the discovery call propagate unchanged.
    """
    discovered = discover_accounts(connection_ref_id)
    reachable_ids = _flatten_account_ids(discovered.get("accounts"))
    if account_id not in reachable_ids:
        raise AccountNotReachable(account_id)

    account_label = _label_for_account(discovered.get("accounts"), account_id)

    return upsert_scope(
        connection_ref_id,
        account_id=account_id,
        account_label=account_label,
        state=STATE_READY,
        selected_by=selected_by,
        verified_at=datetime.now(tz=timezone.utc),
    )


def _flatten_account_ids(accounts) -> set[str]:
    """Collect every ``id`` string from a (possibly nested) accounts structure.

    Generic: an accounts payload is a list of nodes; a node may carry an ``id``
    and an optional ``children`` list (the hierarchy the discovery call returned).
    Core walks it structurally without knowing the level names (AD-2).
    """
    ids: set[str] = set()

    def _walk(nodes) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            nid = node.get("id")
            if isinstance(nid, str) and nid:
                ids.add(nid)
            _walk(node.get("children"))

    _walk(accounts)
    return ids


def _label_for_account(accounts, account_id: str) -> str | None:
    """Return the label of the node whose id == account_id (best effort)."""
    result: list[str | None] = [None]

    def _walk(nodes) -> bool:
        if not isinstance(nodes, list):
            return False
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("id") == account_id:
                lbl = node.get("label")
                result[0] = lbl if isinstance(lbl, str) else None
                return True
            if _walk(node.get("children")):
                return True
        return False

    _walk(accounts)
    return result[0]


# ---------------------------------------------------------------------------
# AC3 -- bounded TRIAL pull (last TRIAL_PULL_DAYS days) via the existing queue.
# ---------------------------------------------------------------------------


def trial_window(today: date | None = None) -> tuple[str, str]:
    """Return (date_from, date_to) for the bounded trial: last TRIAL_PULL_DAYS days.

    The window ends YESTERDAY (like _trigger_pull's default) and spans
    TRIAL_PULL_DAYS days. Returned as ISO-8601 strings.
    """
    today = today or date.today()
    date_to = today - timedelta(days=1)
    date_from = date_to - timedelta(days=TRIAL_PULL_DAYS - 1)
    return date_from.isoformat(), date_to.isoformat()


def enqueue_trial_pull(connection_ref_id: str, *, requested_by: str) -> dict:
    """Enqueue the bounded TRIAL pull through the EXISTING enqueue_pull path.

    No new pull semantics: this is a normal pull over the trial window. The
    enqueue guard (AC4) allows it because selection has already written a
    ``ready`` scope. Returns whatever enqueue_pull returns (job dict or refusal).
    """
    from core.queue import enqueue_pull  # noqa: PLC0415

    date_from, date_to = trial_window()
    return enqueue_pull(
        connection_ref_id,
        date_from,
        date_to,
        requested_by=requested_by,
    )


# ---------------------------------------------------------------------------
# AC3 -- windowed backfill (1..365 days -> <=31-day windows).
# ---------------------------------------------------------------------------


def validate_backfill_days(days) -> int:
    """Coerce + validate the backfill span to an int in [1, 365].

    Raises BackfillDaysInvalid on anything outside the range or non-integer.
    """
    try:
        days_int = int(days)
    except (TypeError, ValueError):
        raise BackfillDaysInvalid(days)
    if days_int < BACKFILL_MIN_DAYS or days_int > BACKFILL_MAX_DAYS:
        raise BackfillDaysInvalid(days_int)
    return days_int


def compute_backfill_windows(days, today: date | None = None) -> list[dict]:
    """Split a *days*-long backfill (ending yesterday) into <=31-day windows.

    Validates 1..365 first (AC3). The most recent window ends YESTERDAY; each
    window spans at most BACKFILL_WINDOW_DAYS days; windows are contiguous and
    cover exactly *days* days with no overlap. Ordered oldest-first.

    Returns a list of {"date_from", "date_to"} ISO-8601 dicts. For days<=31 this
    is a single window; for days=32 it is [31 days][1 day]; days=365 -> 12 windows.
    """
    days_int = validate_backfill_days(days)
    today = today or date.today()
    overall_to = today - timedelta(days=1)
    overall_from = overall_to - timedelta(days=days_int - 1)

    windows: list[dict] = []
    cursor = overall_from
    while cursor <= overall_to:
        window_to = min(cursor + timedelta(days=BACKFILL_WINDOW_DAYS - 1), overall_to)
        windows.append(
            {"date_from": cursor.isoformat(), "date_to": window_to.isoformat()}
        )
        cursor = window_to + timedelta(days=1)
    return windows


def enqueue_backfill(connection_ref_id: str, days, *, requested_by: str) -> list[dict]:
    """Enqueue ONE pull job per backfill window and return the window list (AC3).

    Never auto-triggered -- only an explicit /backfill request reaches here. Each
    window is enqueued through the existing enqueue_pull path (existing dedup
    applies). The returned list carries the enqueue result alongside each window
    so the caller can surface deduplication.
    """
    from core.queue import enqueue_pull  # noqa: PLC0415

    windows = compute_backfill_windows(days)
    results: list[dict] = []
    for window in windows:
        outcome = enqueue_pull(
            connection_ref_id,
            window["date_from"],
            window["date_to"],
            requested_by=requested_by,
        )
        results.append(
            {
                "date_from": window["date_from"],
                "date_to": window["date_to"],
                "job_id": outcome.get("job_id"),
                "state": outcome.get("state"),
                "deduplicated": outcome.get("deduplicated", False),
            }
        )
    return results


# ---------------------------------------------------------------------------
# Typed errors (endpoints map these to HTTP codes; core never raises HTTP here).
# ---------------------------------------------------------------------------


class TopologyError(Exception):
    """Base for account-topology flow errors."""


class NoTopologyError(TopologyError):
    """The module declares no (valid) account_topology (AC1/AC3: 409)."""

    def __init__(self, provider: str):
        self.provider = provider
        super().__init__(f"provider '{provider}' declares no account topology")


class ConnectionNotFound(TopologyError):
    """The connection_ref id does not exist (404)."""

    def __init__(self, connection_ref_id: str):
        self.connection_ref_id = connection_ref_id
        super().__init__(f"connection_ref '{connection_ref_id}' not found")


class AccountNotReachable(TopologyError):
    """The selected account is not in the token's reachable set (422)."""

    def __init__(self, account_id: str):
        self.account_id = account_id
        super().__init__(f"account '{account_id}' is not reachable by this connection")


class BackfillDaysInvalid(TopologyError):
    """The backfill span is outside [1, 365] or not an integer (422)."""

    def __init__(self, days):
        self.days = days
        super().__init__(
            f"days must be an integer in [{BACKFILL_MIN_DAYS}, {BACKFILL_MAX_DAYS}], "
            f"got: {days!r}"
        )
