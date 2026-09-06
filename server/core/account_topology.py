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
  * resolve_selected_account(connection_ref_id[, connector]) -- pull-site hook
  * get_scope(connection_ref_id[, account_id][, connector]) -> dict | None -- one row
  * list_scopes(connection_ref_id) -> list[dict] -- every verified account of one grant
  * ready_accounts_for_connector(connection_ref_id, connector[, conn]) -> list[str]
    -- THE readiness predicate. `queue._resolve_selected_account` calls this one;
    it held a hand-written copy until 2026-08-31.
  * has_ready_scope(connection_ref_id) -> bool  -- enqueue-guard probe (AC4)
  * is_account_ready(connection_ref_id, account_id) -> bool -- account-exact probe
  * enqueue_trial_pull(...)                      -- bounded (3-day) trial via enqueue_pull
  * compute_backfill_windows(days) -> list[dict] -- 1..365 -> <=31-day windows

Env-var account fallbacks (``*_ACCOUNT_ID``) are DEPRECATED and removed per-module
at rollout (the meta-ads stitch + Story 25.7): once a module declares
``account_topology``, ``resolve_selected_account`` is the single source of the
reporting account -- an env-var default is a scoping bypass and must not survive.

Windows/CI note (AI-03): all log/message strings use ASCII-safe characters only.
"""

from __future__ import annotations

import json
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


def account_connector_sql(alias: str = "ca") -> str:
    """SQL predicate: this Source Account belongs to this Connector.

    Takes ONE parameter, a list of connector ids, and is the only correct way to
    ask the question. Four call sites asked it as ``connection_ref.connector_id =
    %s`` instead -- a column that has never existed in any migration, so each of
    them raised ``UndefinedColumn`` on every call, and the Datastream setup
    answered "Source Account not found" to accounts that were right there.

    It could not have existed. One Google consent opens ten Connectors
    (``connection_tools.GOOGLE_SCOPE_CONNECTORS``), so an authorization has no
    single Connector to be keyed by. The account does: migration 210 added
    ``credential_accounts.discovered_for_connector`` precisely so a Search
    Console site could be told from an Analytics property in a table that keys
    both by ``(credential_id, external_account_id)``.

    NULL is accepted, per 210's own rule: rows discovered before that column
    existed carry NULL, and NULL means "unknown", not "none". Narrowing them
    would hide accounts a person can see in Sources.
    """
    return (
        f"({alias}.discovered_for_connector = ANY(%s) "
        f"OR {alias}.discovered_for_connector IS NULL)"
    )


# ---------------------------------------------------------------------------
# ULID helper
# ---------------------------------------------------------------------------


def _mint_scope_id() -> str:
    """Mint a new ULID with 'ascope_' prefix."""
    from ulid import ULID  # noqa: PLC0415

    return f"ascope_{ULID()}"


def _mint_source_account_id() -> str:
    """Mint the opaque stable identity of a discovered provider account."""
    from ulid import ULID  # noqa: PLC0415

    return f"sacct_{ULID()}"


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


def selected_account_pull_arguments(
    provider: str | None, external_account_id: str | None
) -> dict[str, str]:
    """Name the chosen account the way the Connector declares it, or say nothing.

    THE SETUP PATH DID NOT BIND THE SELECTION AT ALL. The scheduled run path binds
    it (``core/queue.py``), the setup path did not: the preview guessed among five
    parameter names and the first collection passed none, so a pull fell back to
    whatever the token points at by default. Measured 2026-08-12 against
    production: every first collection read the consenting identity's own object
    instead of the one the operator picked and the funnel had verified, and
    answered with zero rows -- an empty Datastream with no error anywhere.

    ``account_topology.pull_parameter`` is the single declaration, exactly as it
    is for the run path, so no provider vocabulary appears here and a Connector
    that names its parameter differently is served without a change in this file.
    """
    if not provider or not external_account_id:
        return {}
    topology = get_topology_for_provider(provider) or {}
    declared = topology.get("pull_parameter")
    if not declared:
        if topology.get("selection_level"):
            logger.warning(
                "account_topology: selection_unreachable module=%s declares "
                "selection_level=%r and no pull_parameter -- the operator's "
                "chosen account cannot reach the extraction",
                provider,
                topology.get("selection_level"),
            )
        return {}
    return {str(declared): str(external_account_id)}


def _resolve_verification_callable(provider: str):
    """Return the connector's own access-verification callable, or None.

    Access is a per-product question: one authorization can open several
    products, and each governs a different object with its own rights. Treating
    the enumerated set as the reachable set makes any entity its provider
    declines to list permanently unreachable. The name lives in the module's
    manifest, beside the discovery callable (AD-2).

    Contract: ``fn(connection_ref_id, account_id) -> dict | None``. A dict means
    reachable and carries the canonical ``id`` plus a ``label``; a falsy value
    means refused. Typed connector errors propagate -- a rate limit is not a
    refusal. A module declaring none keeps the enumeration-only behaviour.
    """
    loaded = _resolve_module(provider)
    if loaded is None:
        return None
    topology = get_topology(getattr(loaded, "manifest", None))
    if topology is None:
        return None
    callable_name = (topology.get("verification") or {}).get("callable")
    if not isinstance(callable_name, str) or not callable_name:
        return None
    fn = getattr(loaded.connector_module, callable_name, None)
    return fn if callable(fn) else None


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


def discover_accounts(connection_ref_id: str, module: str | None = None) -> dict:
    """Run the module's discovery callable and return the reachable accounts.

    Returns a dict:
        {"topology": <levels/selection_level>, "accounts": [...]}

    Args:
        connection_ref_id: the authorization to discover through.
        module: WHICH tool of that authorization to discover for. Needed only
            where one authorization opens several -- a Google direct grant is
            stored with provider='google', which declares no topology of its own;
            the properties a person picks from belong to Search Console, or to
            Analytics, or to Ads, and those are different lists. Callers must have
            validated it through `connection_tools.resolve_connection_connector` first.

    Raises:
        NoTopologyError   -- the module declares no (valid) account_topology.
        ConnectionNotFound-- the connection_ref does not exist.
    Any typed connector error (core.pull_errors.ConnectorError) or RateLimitError
    from the discovery call propagates unchanged so the endpoint can map it.
    """
    ref = resolve_connection_ref(connection_ref_id)
    if ref is None:
        raise ConnectionNotFound(connection_ref_id)

    provider = (module or "").strip() or ref["provider"]
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
    # A google_direct row has no nango_connection_id -- there is no Nango
    # connection behind it -- so it is addressed by its own connection_ref id,
    # which resolve_connection_by_nango_id matches as well.
    accounts = discovery_fn(ref["nango_connection_id"] or connection_ref_id)

    # Story 46.4 retired the runtime flag this used to be gated on: reconciling the
    # discovered account set is unconditional. `if True:` kept the shape of a gate
    # that no longer exists, which reads as "there is a condition here" to the next
    # person.
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        # `provider` here is the CONNECTOR actually discovered through -- the
        # requested module when one was given, the connection's provider
        # otherwise. Recording it is what lets a later reader tell a Search
        # Console site from an Analytics property in a table that keys both by
        # `(credential_id, external_account_id)` alone (migration 210).
        reconcile_discovered_accounts(
            connection_ref_id, accounts, conn, discovered_for_connector=provider
        )
        conn.commit()
    return {"topology": topology, "accounts": accounts}


def reconcile_discovered_accounts(
    connection_ref_id: str, accounts, conn, *, discovered_for_connector: str | None = None
) -> None:
    """Replace the available account set and invalidate stale exposure atomically."""
    account_ids = _flatten_account_ids(accounts)
    with conn.cursor() as cur:
        # Absence from a list is not evidence of absence: an entity whose access
        # was PROVEN keeps its availability until a verification fails, or the
        # next discovery would erase what enumeration cannot see.
        cur.execute(
            """
            UPDATE app.credential_accounts a SET available = FALSE
             WHERE a.credential_id = %s
               AND NOT EXISTS (
                     SELECT 1 FROM app.connection_account_scope s
                      WHERE s.connection_ref_id = a.credential_id
                        AND s.account_id = a.external_account_id
                        AND s.verified_at IS NOT NULL
                   )
            """,
            (connection_ref_id,),
        )
        for account_id in sorted(account_ids):
            source_account_id = _mint_source_account_id()
            account_label = _label_for_account(accounts, account_id)
            cur.execute(
                "INSERT INTO app.credential_accounts "
                "(source_account_id, credential_id, external_account_id, label, available, "
                "discovery_generation, last_seen_at, discovered_for_connector) "
                "VALUES (%s, %s, %s, %s, TRUE, 1, NOW(), %s) "
                "ON CONFLICT (credential_id, external_account_id) DO UPDATE SET "
                "source_account_id = app.credential_accounts.source_account_id, "
                "label = EXCLUDED.label, available = TRUE, discovery_generation = "
                "app.credential_accounts.discovery_generation + 1, last_seen_at = NOW(), "
                # COALESCE, not EXCLUDED: a caller that did not name the
                # Connector must not erase the one an earlier discovery knew.
                "discovered_for_connector = COALESCE("
                "EXCLUDED.discovered_for_connector, "
                "app.credential_accounts.discovered_for_connector)",
                (
                    source_account_id,
                    connection_ref_id,
                    account_id,
                    account_label,
                    discovered_for_connector,
                ),
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


def get_scope(
    connection_ref_id: str,
    account_id: str | None = None,
    *,
    connector: str | None = None,
) -> dict | None:
    """Return one scope row for a connection as a dict, or None.

    With *account_id*, the row for THAT account. Without it, the most recently
    verified one -- an authorization can hold several since migration 211, and a
    caller that names none is asking a question that no longer has a single
    answer. Use :func:`list_scopes` when the whole set is the question.

    *connector* narrows the unnamed branch, and ONLY it. One Google consent
    covers Search Console, Analytics and Ads, so "the most recently verified
    account of this connection" hands a `sc-domain:` site to an Analytics pull
    -- the failure measured on 2026-08-01 (`data-path.md`, Desaccord 3). The
    connector dimension is not on the scope row: it is
    `app.credential_accounts.discovered_for_connector` (migration 210), joined
    on `(credential_id, external_account_id)`, which this table keys as
    `(connection_ref_id, account_id)`. NULL is accepted there, per 210's own
    rule -- pre-210 rows mean "unknown", not "none".

    Narrowing the NAMED branch would do the opposite of a repair: the caller
    already holds the account it wants (`_operating_context_kwargs` reads its
    `selection_path`), and hiding the row would drop an ancestor the provider
    requires. So the argument is ignored there, deliberately.
    """
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            if account_id is None and connector:
                cur.execute(
                    f"""
                    SELECT s.id, s.connection_ref_id, s.account_id, s.account_label,
                           s.state, s.verified_at, s.selected_by, s.created_at,
                           s.updated_at
                    FROM app.connection_account_scope s
                    LEFT JOIN app.credential_accounts ca
                           ON ca.credential_id = s.connection_ref_id
                          AND ca.external_account_id = s.account_id
                    WHERE s.connection_ref_id = %s
                      AND {account_connector_sql()}
                    ORDER BY (s.state = 'ready') DESC, s.verified_at DESC NULLS LAST
                    LIMIT 1
                    """,  # noqa: S608 -- constant predicate, one bound parameter
                    (connection_ref_id, [connector]),
                )
            elif account_id is None:
                cur.execute(
                    """
                    SELECT id, connection_ref_id, account_id, account_label, state,
                           verified_at, selected_by, created_at, updated_at
                    FROM app.connection_account_scope
                    WHERE connection_ref_id = %s
                    ORDER BY (state = 'ready') DESC, verified_at DESC NULLS LAST
                    LIMIT 1
                    """,
                    (connection_ref_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT id, connection_ref_id, account_id, account_label, state,
                           verified_at, selected_by, created_at, updated_at
                    FROM app.connection_account_scope
                    WHERE connection_ref_id = %s AND account_id = %s
                    """,
                    (connection_ref_id, account_id),
                )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [desc[0] for desc in cur.description]
    return _row_to_scope(cols, row)


def list_scopes(connection_ref_id: str) -> list[dict]:
    """Every selected + verified account of one authorization.

    The plural read migration 211 made possible: one Google consent legitimately
    holds a Search Console site, a GA4 property and an Ads customer at once, and
    before 211 a UNIQUE index let it hold exactly one of them.
    """
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, connection_ref_id, account_id, account_label, state,
                       verified_at, selected_by, created_at, updated_at
                FROM app.connection_account_scope
                WHERE connection_ref_id = %s
                ORDER BY account_id NULLS LAST
                """,
                (connection_ref_id,),
            )
            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description]
    return [_row_to_scope(cols, row) for row in rows]


def ready_accounts_for_connector(
    connection_ref_id: str, connector: str | None = None, conn=None
) -> list[str]:
    """Every VERIFIED account of one authorization that belongs to one Connector.

    The plural, connector-aware read the fallback needs. `list_scopes` answers
    "what did this consent verify"; this answers "which of those can THIS
    Connector read", which is a different question the moment one OAuth screen
    opens several tools -- a Google grant covers `gsc`, `google-analytics`,
    `google-ads` and four more, and each returns a different account space.

    The connector lives on `app.credential_accounts.discovered_for_connector`
    (migration 210), never on the scope row; the join is one-to-one because
    `pk_credential_accounts` is `(credential_id, external_account_id)`. NULL is
    accepted (210: "unknown", not "none"), so a pre-210 account still resolves
    for whichever Connector asks -- which is exactly the behaviour it had.

    Ordered most-recently-verified first, so a caller that takes the head of a
    one-element list gets the same account it always got. Any DB failure raises:
    this is the input of a REFUSAL, and a swallowed error here would answer
    "no ambiguity" to a question that was never asked.

    THE ONE OWNER OF THIS PREDICATE (2026-08-31). `queue._ready_accounts_of_connector`
    re-spelled this SQL on the caller's cursor, arguing in its docstring that it
    "cannot call it: that one owns its connection". It can: `conn` is a parameter,
    and passing it keeps the read on the connection the pull is being decided
    under. The copy is gone; this function answers both the topology reads and the
    queue's, so the enqueue gate and the extraction cannot gate one account and
    read another.
    """

    def _read(_conn) -> list[str]:
        predicate = f"AND {account_connector_sql()}" if connector else ""
        params: list = [connection_ref_id, STATE_READY]
        if connector:
            params.append([connector])
        with _conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT s.account_id, ca.discovered_for_connector
                FROM app.connection_account_scope s
                LEFT JOIN app.credential_accounts ca
                       ON ca.credential_id = s.connection_ref_id
                      AND ca.external_account_id = s.account_id
                WHERE s.connection_ref_id = %s
                  AND s.state = %s
                  AND s.account_id IS NOT NULL
                  {predicate}
                ORDER BY s.verified_at DESC NULLS LAST
                """,  # noqa: S608 -- constant predicate, bound parameters only
                tuple(params),
            )
            rows = cur.fetchall()
        seen: list[str] = []
        for row in rows:
            account = row[0]
            if account and account not in seen:
                seen.append(account)
        return seen

    if conn is not None:
        return _read(conn)
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as own_conn:
        return _read(own_conn)


def is_account_ready(connection_ref_id: str, account_id: str, conn=None) -> bool:
    """True when THAT account of this authorization is selected and verified.

    The account-exact form of :func:`has_ready_scope`. The enqueue guard needs
    it: with several accounts under one consent, "this credential has a ready
    account" no longer implies "the account this Datastream reads is ready".
    Any DB failure returns False -- fail closed, as the credential-wide probe
    does.
    """

    def _probe(_conn) -> bool:
        with _conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM app.connection_account_scope
                WHERE connection_ref_id = %s AND account_id = %s AND state = %s
                LIMIT 1
                """,
                (connection_ref_id, account_id, STATE_READY),
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
            "account_topology: is_account_ready probe failed conn=%s account=%s: %s",
            connection_ref_id,
            account_id,
            exc,
        )
        return False


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
    selection_path: list[dict] | None = None,
) -> dict:
    """UPSERT the scope row for ONE account of a connection and return it.

    ``selection_path`` is the account's ANCESTRY as discovery returned it, root
    first (migration 254). Several providers cannot act on a leaf without the
    branch it hangs from -- a Google Ads child customer needs its MANAGER as
    `login-customer-id` -- and the hierarchy was walked and then discarded. An
    empty list is the honest answer for an account the provider does not
    enumerate: it has no ancestry to read.

    One row per (connection, account) since migration 211: re-selecting the SAME
    account updates its row, selecting a SECOND account adds one instead of
    overwriting the first. The previous conflict target was the connection
    alone, so choosing a Google Ads customer silently un-selected the Search
    Console site chosen an hour earlier -- one consent, one account, for every
    tool it opened. The trail stays in audit_log either way.

    The pending row (``account_id`` NULL, "a selection is expected here") is
    still one per connection, and has its own partial unique index.
    """
    from core.db import get_connection  # noqa: PLC0415

    conflict_target = (
        "(connection_ref_id) WHERE account_id IS NULL"
        if account_id is None
        else "(connection_ref_id, account_id) WHERE account_id IS NOT NULL"
    )
    scope_id = _mint_scope_id()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO app.connection_account_scope
                    (id, connection_ref_id, account_id, account_label, state,
                     verified_at, selected_by, selection_path, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, now(), now())
                ON CONFLICT {conflict_target} DO UPDATE
                    SET account_id    = EXCLUDED.account_id,
                        account_label = EXCLUDED.account_label,
                        state         = EXCLUDED.state,
                        verified_at   = EXCLUDED.verified_at,
                        selected_by   = EXCLUDED.selected_by,
                        selection_path = EXCLUDED.selection_path,
                        updated_at    = now()
                RETURNING id, connection_ref_id, account_id, account_label, state,
                          verified_at, selected_by, selection_path, created_at, updated_at
                """,  # noqa: S608 -- conflict_target is one of two literals above
                (
                    scope_id,
                    connection_ref_id,
                    account_id,
                    account_label,
                    state,
                    verified_at,
                    selected_by,
                    json.dumps(selection_path or []),
                ),
            )
            row = cur.fetchone()
            cols = [desc[0] for desc in cur.description]
        conn.commit()
    return _row_to_scope(cols, row)


def resolve_selected_account(
    connection_ref_id: str, *, connector: str | None = None
) -> str | None:
    """Return the opaque selected account_id for a connection, or None (AC6).

    The generic pull-site hook: connectors/dispatch call this to read the account
    the onboarding flow selected + verified, instead of an ``*_ACCOUNT_ID`` env
    var. Only a ``state='ready'`` scope resolves an account -- a pending/absent
    scope returns None so nothing runs against an unverified account. Env-var
    account fallbacks are DEPRECATED and removed per-module at rollout (meta-ads
    stitch + Story 25.7).

    THIS IS THE CREDENTIAL-WIDE ANSWER AND IT IS A FALLBACK. Since migration 211
    the account a pull reads belongs to the Datastream
    (``app.datastreams.source_account_id``), and ``queue._resolve_selected_account``
    asks it first. An authorization with several verified accounts has no single
    answer to give here, so this returns its most recently verified one -- right
    for the legacy per-connection path, arbitrary for anything that knows its
    Datastream.

    *connector* is what keeps "arbitrary" from becoming "wrong across tools":
    a connector that names itself gets the most recently verified account OF
    ITS OWN, never a Search Console site handed to an Analytics pull. Every
    connector calling this knows its own name statically, so there is no reason
    for any of them to omit it.
    """
    scope = get_scope(connection_ref_id, connector=connector)
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
    module: str | None = None,
) -> dict:
    """Verify access to *account_id*, then persist a ``ready`` scope row.

    Access verification is a MINIMAL read against THAT account via the module's
    discovery path: we re-run discovery and confirm the requested account is in
    the reachable set. This proves the token can actually reach the selected
    account (no blind trust of a client-supplied id) without inventing a
    provider-specific access-check call (AD-2). On success the scope is UPSERTed
    to ``state='ready'`` with ``verified_at`` set to now.

    Returns the persisted scope dict.

    Args:
        module: WHICH tool of the authorization the account belongs to, for the
            same reason ``discover_accounts`` takes it. Omitting it here while
            passing it to discovery is not a small asymmetry: a Google direct
            grant is stored with provider='google', which declares no topology,
            so verification raised NoTopologyError for every property a person
            had just been offered. Listing worked, choosing did not.

    Raises:
        NoTopologyError    -- the module declares no topology.
        ConnectionNotFound -- the connection_ref does not exist.
        AccountNotReachable-- *account_id* is not in the reachable set.
    Typed connector errors from the discovery call propagate unchanged.
    """
    discovered = discover_accounts(connection_ref_id, module=module)
    reachable_ids = _flatten_account_ids(discovered.get("accounts"))
    account_label = _label_for_account(discovered.get("accounts"), account_id)

    if account_id not in reachable_ids:
        # Enumeration is a convenience; authorization is the provider's answer.
        # An entity a token can read but the provider will not list was
        # unreachable forever, so the module is asked instead of the list.
        provider = (module or "").strip()
        if not provider:
            # Only when the caller did not name the tool: a verification that
            # cannot identify the PRODUCT has nothing to ask, and a lookup that
            # fails must fall back to the enumeration answer rather than raise
            # something the caller does not expect.
            try:
                provider = (resolve_connection_ref(connection_ref_id) or {}).get("provider") or ""
            except Exception:  # noqa: BLE001
                provider = ""
        verifier = _resolve_verification_callable(provider) if provider else None
        verified = None
        if verifier is not None:
            verified = verifier(connection_ref_id, account_id)
        if not verified:
            raise AccountNotReachable(account_id)
        if isinstance(verified, dict):
            account_label = str(verified.get("label") or account_id)
            # The canonical identity is what is queryable; what was typed is not.
            resolved = verified.get("id")
            if isinstance(resolved, str) and resolved:
                account_id = resolved
        else:
            account_label = account_label or account_id

    scope = upsert_scope(
        connection_ref_id,
        account_id=account_id,
        account_label=account_label,
        state=STATE_READY,
        selected_by=selected_by,
        verified_at=datetime.now(tz=timezone.utc),
        selection_path=account_selection_path(discovered.get("accounts"), account_id),
    )

    # Reaching the account IS the successful verified read the sticky
    # `populate_failed` flag waits for. Without this the flag outlived the proof:
    # the authorization answered, the account verified, and every run of it was
    # still refused `access_denied`.
    from core.verification import clear_connection_health_red  # noqa: PLC0415

    clear_connection_health_red(connection_ref_id, evidence=f"account_verified:{account_id}")

    # A PROVEN ACCOUNT IS A SOURCE ACCOUNT. The scope row records the decision;
    # `app.credential_accounts` is what every surface READS -- the Sources page,
    # the wizard's account question, the Datastream's foreign key. Verifying an
    # entity the provider will not enumerate and leaving it out of that table
    # would prove the access and still offer no way to use it.
    if account_id not in reachable_ids:
        try:
            from core.db import get_connection  # noqa: PLC0415

            with get_connection() as conn:
                reconcile_discovered_accounts(
                    connection_ref_id,
                    [{"id": account_id, "label": account_label}],
                    conn,
                    discovered_for_connector=module,
                )
                conn.commit()
        except Exception:  # noqa: BLE001 -- the verification stands on its own
            logger.warning(
                "account_topology: verified account not projected connection=%s",
                connection_ref_id,
            )
    return scope


def account_selection_path(accounts, account_id: str) -> list[dict[str, str]]:
    """The ANCESTRY of a selected account, root first, leaf last.

    A selection is a path, not a leaf: several products cannot act on an entity
    without the branch it hangs from, and the hierarchy was walked and then
    discarded. Which level supplies which call parameter is declared in the
    manifest (``operating_context``); core never learns a level's meaning (AD-2).

    Returns [] when the account is not in the tree -- an entity proven by
    `verification` has no ancestry to read.
    """
    trail: list[dict[str, str]] = []

    def _walk(nodes, ancestry: list[dict[str, str]]) -> bool:
        if not isinstance(nodes, list):
            return False
        for node in nodes:
            if not isinstance(node, dict):
                continue
            nid = node.get("id")
            here = ancestry + [{
                "id": str(nid) if isinstance(nid, str) else "",
                "label": str(node.get("label") or nid or ""),
            }]
            if isinstance(nid, str) and nid == account_id:
                trail.extend(here)
                return True
            if _walk(node.get("children"), here):
                return True
        return False

    _walk(accounts, [])
    return trail


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


def _connection_auth_path(connection_ref_id: str) -> str | None:
    """The auth path of a connection, or None when it cannot be read."""
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT auth_path FROM app.connection_ref WHERE id = %s",
                (connection_ref_id,),
            )
            row = cur.fetchone()
        return row[0] if row else None
    except Exception:  # noqa: BLE001 -- best effort; caller keeps prior behaviour
        logger.warning("account_topology: auth_path lookup failed", exc_info=True)
        return None


def enqueue_trial_pull(connection_ref_id: str, *, requested_by: str) -> dict:
    """Enqueue the bounded TRIAL pull through the EXISTING enqueue_pull path.

    No new pull semantics: this is a normal pull over the trial window. The
    enqueue guard (AC4) allows it because selection has already written a
    ``ready`` scope. Returns whatever enqueue_pull returns (job dict or refusal).

    NOT ENQUEUED FOR A MULTI-TOOL CREDENTIAL
    A pull job carries a connection and, optionally, a datastream -- and the
    module is read from one of those two. A Google direct grant is stored as
    provider='google' and opens seven tools, so a trial pull with no datastream
    names no module: it can only dead-letter with "no pull function registered".
    Queueing a job that cannot run is worse than not queueing it, so the reason
    is returned instead. The verification that matters already happened -- the
    account was reached during selection.
    """
    from core.queue import enqueue_pull  # noqa: PLC0415

    if _connection_auth_path(connection_ref_id) == "google_direct":
        return {
            "state": "skipped",
            "reason": "multi_tool_credential_needs_a_datastream",
        }

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
