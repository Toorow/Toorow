"""toorow -- Platform Postgres connection helper (Story 2.4, T5.3).

Thin wrapper around psycopg (v3, sync) for the platform-db (app schema).
Reads PLATFORM_DB_URL env var at call time -- no module-level connection pool
(stateless module pattern, same as warehouse.py and nango_client.py).

Design decision (recorded per Dev Notes):
  psycopg[binary] v3 (sync) -- consistent with the existing sync pattern in
  audit.py and nango_client.py's _run_coro wrapper. asyncpg would require
  an async context that conflicts with Starlette's sync route handlers without
  an asyncio.run() wrapper (nested-loop risk). psycopg 3.3.4 is already in venv.

AD-3: no token columns. connection_ref stores only nango_connection_id.
Windows/CI note (L-3): all log output uses ASCII-safe strings only.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

PLATFORM_DB_URL_ENV = "PLATFORM_DB_URL"

# psycopg imported at module level so tests can patch core.db.psycopg.
try:
    import psycopg as _psycopg
except ImportError:  # pragma: no cover
    _psycopg = None  # type: ignore[assignment]


def _db_url() -> str:
    """Return the Postgres DSN, reading env var at call time.

    Priority:
      1. PLATFORM_DB_URL env var (explicit override).
      2. Default: platform-db dev credentials from docker-compose.
    """
    override = os.environ.get(PLATFORM_DB_URL_ENV, "").strip()
    if override:
        return override
    password = os.environ.get("PLATFORM_DB_PASSWORD", "connector_dev_only")
    return f"postgresql://connector:{password}@localhost:5432/connector"


def _connect_timeout_seconds() -> int:
    """Bounded TCP connect timeout (seconds), env-overridable.

    Without it, psycopg falls back to the OS TCP timeout (~21 s+ per attempt on
    Windows). Every best-effort ``get_connection()`` call in a test run against an
    absent local Postgres then serialises those waits -- the root cause of the
    multi-hour full-suite runs at ~0 CPU (AI-60, diagnosed 2026-07-21: pytest stuck
    in SynSent on ::1:5432). Production (Supabase pooler) connects well under this
    bound; failures surface in seconds instead of dozens of them.
    """
    raw = os.environ.get("PLATFORM_DB_CONNECT_TIMEOUT", "5").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


@contextmanager
def get_connection() -> Iterator["_psycopg.Connection"]:
    """Context manager: yield an open psycopg connection and CLOSE it on exit.

    It does NOT commit. The docstring used to say "auto-commit on exit", which
    is the opposite of what the `finally` block does -- it calls `conn.close()`,
    and psycopg rolls back an open transaction on close. A caller that trusted
    the sentence got a function that reported success and left the database
    untouched. Every write must call `conn.commit()` itself, as the usage below
    has always shown.

    Usage:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
            conn.commit()   # required; nothing here does it for you

    Raises:
        EnvironmentError: if psycopg is not installed.
        psycopg.Error: on connection failure.
    """
    if _psycopg is None:  # pragma: no cover
        raise EnvironmentError(
            "psycopg (v3) is not installed. Add psycopg[binary] to server/pyproject.toml."
        )
    url = _db_url()
    if "connect_timeout" in url:
        # A DSN-specified timeout wins (kwargs would override it otherwise).
        conn = _psycopg.connect(url)
    else:
        conn = _psycopg.connect(url, connect_timeout=_connect_timeout_seconds())
    try:
        yield conn
    finally:
        conn.close()


def set_local_access_context(
    conn: Any, identity: str, *, enforce_epic36: bool = False
) -> None:
    """Install server-trusted access context for the current transaction only.

    Kept transaction-local on purpose: callers hand in a connection they do not
    own, sometimes mid-transaction, and a session-scoped setting on a shared
    connection would outlive the work it was installed for. When the seam owns
    the connection -- `request_connection` below -- the context is installed for
    the whole session instead, because there the two lifetimes are the same.
    """
    if not identity or identity == "anonymous":
        raise ValueError("a non-anonymous identity is required")
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('toorow.identity', %s, true)", (identity,))
        cur.execute(
            "SELECT set_config('toorow.enforce_epic36', %s, true)",
            ("on" if enforce_epic36 else "off",),
        )


def _auth_is_disabled() -> bool:
    return os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower() == "disabled"


def install_access_context(conn: Any, identity: str) -> None:
    """Arm the Epic-36 floor on a connection THIS process owns, for its session.

    Session-scoped (``set_config(..., false)``) and committed immediately, and
    both halves are load-bearing. The transaction-local form reverts at the end
    of the transaction, so a handler that writes, commits, and then reads again
    -- the ordinary shape of every command endpoint -- ran its second half with
    `toorow.enforce_epic36` unset. The policy predicate is
    ``current_setting(...) IS DISTINCT FROM 'on' OR app.epic36_has_resource_access(...)``,
    which is unconditionally TRUE in that state: the floor silently dropped
    halfway through the request. Committing the setting at acquisition also
    survives a later ROLLBACK, which a plain session-scoped SET would not.

    The connection is fresh when this runs, so the commit here commits nothing
    else.

    Auth-disabled self-host has exactly one compatibility subject, `anonymous`:
    a single local operator with no `app.org_members` row to resolve against.
    Arming it would turn every local read into a 404. That carve-out already
    existed in `admin_api`, `rendus_api` and `query_specs_api`; it is not
    invented here, and it disappears the moment auth is enabled.
    """
    # A blank-but-not-empty identity is the worst of the three: it passes a
    # truthiness check, reaches `toorow.identity`, and matches no `org_members`
    # row -- an armed floor over an identity nobody can be.
    identity = (identity or "").strip()
    if _auth_is_disabled() and (not identity or identity == "anonymous"):
        return
    if not identity or identity == "anonymous":
        # AC2: no silent fallback to an empty context. A missing GUC makes the
        # policy permissive, so "carry on unarmed" is the exact defect being
        # removed -- fail closed and loudly instead.
        raise ValueError(
            "a resolved, non-anonymous identity is required to open a "
            "request-scoped connection while authentication is enabled"
        )
    with conn.cursor() as cur:
        _become_the_application_role(cur, conn)
        cur.execute("SELECT set_config('toorow.identity', %s, false)", (identity,))
        cur.execute("SELECT set_config('toorow.enforce_epic36', 'on', false)")
    conn.commit()
    _refuse_a_connection_that_forgets_its_context(conn, identity)


#: Le role sous lequel une requete doit s'executer. Il n'a pas BYPASSRLS, ce qui
#: est tout son interet.
APP_ROLE = os.environ.get("TOOROW_APP_ROLE", "connector").strip() or "connector"


def _become_the_application_role(cur: Any, conn: Any) -> None:
    """`SET ROLE`, parce que le DSN n'a pas besoin de changer pour que RLS morde.

    LE PROBLEME : en production l'application se connecte en `postgres`, qui porte
    `rolbypassrls = true`. Toutes les politiques sont donc ignorees, quoi qu'elles
    disent. La reponse ecrite jusqu'ici etait de changer le DSN pour un role sans
    BYPASSRLS -- ce qui demande de creer un mot de passe, une version de secret et
    un deploiement.

    Rien de tout cela n'est necessaire. `postgres` est MEMBRE de `connector`
    (`pg_has_role('postgres','connector','MEMBER')` -> true, mesure 2026-08-05),
    donc la session peut simplement DEVENIR `connector` apres s'etre connectee.
    Les requetes suivantes s'executent sous un role sans BYPASSRLS et les 71
    politiques d'`app` mordent -- sans nouveau credential, sans toucher au secret,
    sans revision a deployer, et donc sans rien a annuler si on veut revenir.

    Deja ce role ? C'est le cas en local, ou le DSN de test pointe deja
    `connector` : `SET ROLE` vers soi-meme est un no-op, on ne le demande pas.

    Pas de role du tout ? Un deploiement self-hosted peut ne pas l'avoir. On
    n'echoue pas : le plancher applicatif (`toorow.enforce_epic36`, arme juste en
    dessous) reste pose, et c'est exactement la posture d'avant ce changement. La
    perte est nommee par `effective_role_bypasses_rls()` plutot que subie.
    """
    cur.execute("SELECT current_user")
    if (cur.fetchone() or [None])[0] == APP_ROLE:
        return
    try:
        cur.execute(f'SET ROLE "{APP_ROLE}"')
    except Exception as exc:  # noqa: BLE001 -- voir docstring : on nomme, on ne casse pas
        logger.warning(
            "access context: SET ROLE %s refuse (%s) -- les politiques RLS seront "
            "ignorees si le role connecte porte BYPASSRLS",
            APP_ROLE,
            exc,
        )
        # LA DEGRADATION ANNONCEE N'EXISTAIT PAS. Postgres AVORTE la transaction
        # sur l'instruction echouee : le `set_config` juste apres levait alors
        # `InFailedSqlTransaction`, et la connexion restait inutilisable pour tout
        # le reste de la requete. Mesure du 2026-08-07 : sur ce deploiement, TOUS
        # les endpoints batis sur `request_connection` repondaient 500 avec
        # « current transaction is aborted » -- dont l'emission de l'adresse
        # entrante, dernier pas du parcours.
        #
        # La connexion est fraiche ici (l'acquisition le dit juste au-dessus),
        # donc ce rollback ne jette que l'echec lui-meme. C'est ce qui rend la
        # posture nommee par la docstring reelle plutot que decrite.
        conn.rollback()


def effective_role_bypasses_rls(conn: Any) -> bool | None:
    """Le role SOUS LEQUEL on execute ignore-t-il RLS ? None si indeterminable.

    `current_user` et non `session_user` : c'est le premier qui decide de RLS
    apres un `SET ROLE`, et confondre les deux ferait dire « isole » a une session
    qui ne l'est pas.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        row = cur.fetchone()
    if not isinstance(row, (tuple, list)) or not row:
        return None
    return row[0] if isinstance(row[0], bool) else None


class PooledConnectionRefused(RuntimeError):
    """The connection does not keep a session setting across a commit."""


def _refuse_a_connection_that_forgets_its_context(conn: Any, identity: str) -> None:
    """Read the floor back AFTER the commit, and refuse the connection if it fell.

    THE WHOLE DESIGN ABOVE ASSUMES ONE CLIENT CONNECTION KEEPS ONE BACKEND, and
    nothing checked it. Measured in production on 2026-08-05:

        SELECT usename, application_name FROM pg_stat_activity
        WHERE backend_type = 'client backend';
        -- -> usename=postgres, application_name='Supavisor'

    The application talks to Postgres through a pooler. In SESSION pooling that
    assumption holds. In TRANSACTION pooling the backend is handed back after
    every transaction: the session setting committed just above is gone for the
    next statement, and the backend that carried it goes to somebody else. The
    floor would drop mid-request, silently, and a context installed for one
    organization could be inherited by another's transaction -- the AI-125 class
    (silent cross-tenant attachment) reintroduced at the connection layer.

    Nothing else detects it. The revision boots, every page renders, and the
    policies pass everything exactly as they do today; only the reason changes.
    `test_request_connection_rls_pg.py` cannot see it either, because a
    disposable Postgres has no pooler in front of it.

    So the floor checks ITSELF, on the connection it is actually running on, in a
    NEW transaction -- which is precisely where a transaction-mode pooler would
    have swapped the backend. One round-trip per request-scoped connection, and
    it converts an invisible isolation hole into a loud refusal.

    This is also why it must not be a warning: a caller that gets a connection
    back keeps using it. Refusing is the only answer that cannot be ignored.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_setting('toorow.enforce_epic36', true), "
            "current_setting('toorow.identity', true)"
        )
        answer = cur.fetchone()
    # UNE REPONSE QUI N'EN EST PAS UNE N'EST PAS UN VERDICT. Contre un vrai
    # Postgres ce SELECT rend toujours UNE ligne de DEUX colonnes ; une doublure
    # de test qui ne scripte pas cette requete rend `()` ou un MagicMock. Traiter
    # ce cas comme « plancher tombe » ferait echouer une trentaine de tests qui ne
    # parlent pas de pooling, et surtout ferait dire a la garde quelque chose
    # qu'elle n'a pas mesure. Elle se tait donc explicitement ici -- ce qui ne
    # relache rien en production, ou l'interlocuteur est toujours un vrai serveur.
    if not isinstance(answer, (tuple, list)) or len(answer) != 2:
        return
    armed, carried = answer
    if not all(isinstance(value, (str, type(None))) for value in (armed, carried)):
        return
    if armed == "on" and carried == identity:
        return
    raise PooledConnectionRefused(
        "the access context did not survive its own commit on this connection: "
        f"enforce_epic36={armed!r} identity={carried!r}. The row-level floor is "
        "session-scoped by design, so this connection cannot isolate anything -- "
        "almost certainly a TRANSACTION-mode pooler in front of Postgres "
        "(Supavisor port 6543). Use a session-mode or direct DSN. Refusing rather "
        "than serving rows without isolation."
    )


@contextmanager
def request_connection(identity: str) -> Iterator[Any]:
    """Yield a connection that already carries the caller's access context.

    THE ACQUISITION IS THE PLACE. `get_connection` has 573 import sites and one
    job -- open and close -- so every module that wanted the floor had to
    remember to arm it, and 113 of the 118 that read an RLS-forced table never
    did. Arming a *different* connection buys nothing either: the authorization
    check usually opens, uses and closes its own, and the handler then opens a
    fresh unarmed one. This seam removes both mistakes by making the armed
    connection the one you get.

    IT IS ALSO THE PLACE FOR THE IDENTITY TRANSLATION. The floor compares
    `toorow.identity` against `app.org_members.identity`, which carries the
    CANONICAL identity (`person_<ULID>`). The HTTP path already resolves it; MCP
    tools passed the raw OIDC subject, which matches no membership row -- so a
    member read an empty database and their tools answered "not found" about
    their own objects. Translating here rather than in every caller is exactly
    the argument of the paragraph above: whoever arms the floor is the one who
    knows WHO it is armed for.

    The translation is a no-op for an already-canonical identity, and it never
    creates a person: an unknown subject passes through unchanged and is refused
    downstream, exactly as before.
    """
    from core.identity_bridge import canonical_identity  # noqa: PLC0415

    with get_connection() as conn:
        install_access_context(conn, canonical_identity(identity, conn))
        yield conn


@contextmanager
def background_connection(reason: str) -> Iterator[Any]:
    """Yield a connection that deliberately does NOT isolate, and names why.

    Scheduler and queue run without a human identity and legitimately work for
    several organizations at once -- the nightly dispatch reads every armed
    Datastream. RLS is the *second* barrier, the net under the access point, and
    these paths are not requests: they were never inside the scope epic 21 states.
    They already behaved this way (`enforce_epic36` defaults to False, and
    `scheduler.py` has zero calls); what was missing is that a reader could not
    tell an unisolated path from a forgotten one. Passing the reason is the whole
    point of this function -- it is recorded in the caller, where the choice lives.

    Giving these paths a service identity was considered and refused: it would
    need an `app.org_members` row per organization for an actor who is nobody,
    which fabricates a member in the membership registry.
    """
    if not reason or not reason.strip():
        raise ValueError(
            "background_connection requires a reason: an unisolated path must be "
            "readable as a decision, not as an omission"
        )
    with get_connection() as conn:
        yield conn


#: What ``warehouse_path()`` answers when nothing is declared. An EMPTY database,
#: which is why a caller that cannot tell it from a seeded file reads zeroes as
#: an answer.
IN_MEMORY_WAREHOUSE = ":memory:"


def warehouse_path() -> str:
    """The DuckDB warehouse THIS PROCESS reads, or ``:memory:`` when none is declared.

    Exists so an instrument can print the warehouse the PRODUCT opens instead of
    the one it resolved for itself (AI-305, measured 2026-08-24):
    ``scripts/run_evals.py`` fell back to the seed file it knows about and printed
    ``duckdb_available: True``, while the tool seam beside it resolved
    ``TOOROW_DUCKDB_PATH`` -- unset -- and opened an EMPTY in-memory database. The
    run then scored 9.5% accuracy, blaming the product for a missing variable,
    under a header announcing the warehouse as available. An instrument that
    measures its own copy of a value cannot see that.
    """
    return os.environ.get("TOOROW_DUCKDB_PATH", "").strip() or IN_MEMORY_WAREHOUSE


@contextmanager
def get_warehouse_connection(read_only: bool = False) -> Iterator[Any]:
    """Context manager: yield an open DuckDB warehouse connection.

    Uses TOOROW_DUCKDB_PATH env var if set, or defaults to ':memory:'.

    Args:
        read_only: When True, open the DuckDB database in read-only mode
            (structural AD-8 enforcement for read-only consumers such as the
            schema-context generator). A ':memory:' database cannot be opened
            read-only, so the flag is ignored for the in-memory default.
    """
    import duckdb

    path = warehouse_path()
    if read_only and path != IN_MEMORY_WAREHOUSE:
        conn = duckdb.connect(path, read_only=True)
    else:
        conn = duckdb.connect(path)
    try:
        yield conn
    finally:
        conn.close()
