"""Story 50.7 AC3 -- the sentinel sweep: no bearer material reaches any log.

WHY A SENTINEL AND NOT A CODE REVIEW. The retired path leaked `token[:8]` in three
durable places at once -- a `logger.error`, a persisted audit row and a rate-limit
dictionary key -- and every one of them had been read by a human who did not
notice. A sentinel is read by a string search, which does not get tired.

The three sites are named without line numbers on purpose: they were DELETED, not
moved, so every number this docstring carried pointed past the end of
`rendus_api.py` by 2026-08-07. `rendus_api.py:570` keeps the account in prose.

WHAT MAKES THIS FALSIFIABLE. The bearer is forced to a known value, capture runs at
`DEBUG` on the ROOT logger (so it catches any module that logs, not only the two
this story wrote), and the assertion is on the formatted record AND on its raw args
-- because `logger.info("... %s", secret)` leaves the secret out of `record.msg`
and inside `record.args`, which is exactly how a redaction test passes while the
leak ships.
"""

from __future__ import annotations

import logging
import os

import pytest

# The pepper and the origin, for THIS module's tests only -- a module-level
# `os.environ.setdefault` wrote them for the whole session (AI-377).
from tests.support.render_share_env import render_share_env  # noqa: E402,F401

SENTINEL = "SENTINELBEARER0123456789abcdefghijklmnop"


class Capture(logging.Handler):
    """Every record, at every level, from every logger."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.blobs: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        parts = [str(record.name), str(record.msg)]
        if record.args:
            parts.append(repr(record.args))
        try:
            parts.append(record.getMessage())
        except Exception:  # a bad format string must not hide a leak
            pass
        if record.exc_info and record.exc_info[1] is not None:
            parts.append(repr(record.exc_info[1]))
        self.blobs.append(" ".join(parts))

    @property
    def text(self) -> str:
        return "\n".join(self.blobs)


@pytest.fixture()
def capture():
    handler = Capture()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def _assert_clean(handler: Capture, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in handler.text, (
            f"bearer material {secret[:12]!r}... reached a log record:\n{handler.text}"
        )
        # A prefix is not a redaction. Eight characters of a 256-bit bearer is
        # exactly what the retired path logged.
        assert secret[:8] not in handler.text, (
            f"an 8-character prefix of bearer material reached a log record:\n{handler.text}"
        )


def test_the_pepper_helpers_never_log_the_bearer(capture):
    from core import render_shares  # noqa: PLC0415

    digest = render_shares.bearer_hash(SENTINEL)
    assert SENTINEL not in digest
    _assert_clean(capture, SENTINEL)


def test_the_delivery_url_builder_never_logs_the_bearer(capture):
    from core import render_shares  # noqa: PLC0415

    url = render_shares.build_fragment_delivery_url(SENTINEL)
    assert url.endswith(f"#render={SENTINEL}")
    # The URL is RETURNED, which is the point; it must not be LOGGED.
    _assert_clean(capture, SENTINEL)


def test_a_malformed_bearer_is_refused_without_logging_it(capture):
    """The shortest denial path: refused before any hashing and any database work.

    The assertion is on the bearer THAT WAS SUPPLIED. An earlier version of this
    test passed `bearer="short"` and then asserted on `"short" * 4`, a string the
    code never saw -- so it could not have failed, whatever the code logged. That
    is the shape of guard this epic keeps finding: it reads as protection and
    proves nothing.
    """
    from core import render_shares  # noqa: PLC0415

    too_short = "shortbearer"
    with pytest.raises(render_shares.RenderShareUnavailable):
        render_shares.exchange_bearer(
            conn=None, bearer=too_short, ip_hash=None, client_class="unknown"
        )
    _assert_clean(capture, too_short)


def test_the_rate_limiter_keys_on_hashes_and_logs_nothing(capture):
    """AC9: keyed on the bearer's HMAC prefix and the hashed IP, never on a prefix
    of the plaintext bearer. The retired keys were `token[:8]`."""
    from core import render_shares  # noqa: PLC0415

    render_shares.reset_rate_limits()
    subject = render_shares.session_hash(SENTINEL)
    allowed, _retry = render_shares.check_rate_limit(
        ip_hash=render_shares.client_ip_hash("198.51.100.7"), subject_hash=subject
    )
    assert allowed
    _assert_clean(capture, SENTINEL)
    # And the in-memory key space itself holds no plaintext.
    keys = " ".join(render_shares._RATE_BUCKETS.keys())  # noqa: SLF001
    assert SENTINEL not in keys and SENTINEL[:8] not in keys
    assert "198.51.100.7" not in keys, "the raw client IP became a dictionary key"


def test_the_client_ip_is_stored_only_as_a_hash(capture):
    from core import render_shares  # noqa: PLC0415

    digest = render_shares.client_ip_hash("198.51.100.7")
    assert digest is not None and "198.51.100.7" not in digest and len(digest) == 64
    _assert_clean(capture, "198.51.100.7")


def test_no_source_line_interpolates_a_bearer_slice():
    """The static half of the same guarantee.

    `token[:8]` is the exact expression the retired modules used in three places.
    This asserts the new modules contain no slice of a bearer or session value at
    all -- catching the leak in review rather than in a captured log, which only
    covers the paths a test happens to drive.
    """
    import ast
    import pathlib

    # Parsed, not grepped. These modules DESCRIBE the retired `token[:8]` defect in
    # their docstrings, on purpose -- CLAUDE.md anti-drift rule 3 keeps the trace of
    # what was retired. A text search cannot tell prose from code and would either
    # fail on the documentation or be loosened until it proved nothing. The AST
    # sees only executable subscripts.
    secretish = ("bearer", "token", "session_value", "session")
    root = pathlib.Path(__file__).resolve().parents[2] / "core"
    for name in ("render_shares.py", "render_shares_api.py", "render_shares_console_api.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Slice):
                continue
            target = node.value
            label = getattr(target, "id", None) or getattr(target, "attr", None) or ""
            if any(word in label.lower() for word in secretish):
                offenders.append(f"{label}[...] at line {node.lineno}")
        assert not offenders, f"{name} slices bearer material in code: {offenders}"


# ---------------------------------------------------------------------------
# AC3's central clause, DRIVEN. The five real outcome paths, against a real
# database, through the real HTTP handlers, with a sentinel bearer that actually
# RESOLVES TO A SHARE.
#
# Why the rest of this file was not enough. Every test above either never touches
# the database or drives a refusal that returns before the bearer is hashed. A
# `logger.info("bearer=%s", bearer)` on the first line of `exchange_bearer` passes
# all of them -- so the guarantee AC3 states was real in the code and unguarded by
# the tests. This section drives success, denial, expiry, revocation and rate
# limiting, and asserts on the persisted access-event rows as well as on the logs.
# ---------------------------------------------------------------------------

_DSN = os.environ.get("TEST_POSTGRES_DSN", "")


def _sentinel(tag: str, run: str) -> str:
    """43 URL-safe characters -- the shape `secrets.token_urlsafe(32)` produces.

    Anything shorter is refused by the length gate before it is ever hashed, which
    is how a sweep ends up covering only the path that cannot leak.

    The per-run suffix is not decoration: `app.render_shares.bearer_hash` is
    UNIQUE, so a fixed sentinel makes the second run of this test die in its
    fixture. The string is still a sentinel -- the sweep searches for the exact
    value that was supplied on this run.
    """
    body = "SENTINEL" + tag + run
    return (body + "Z" * 43)[:43]


@pytest.fixture()
def driven(capture):
    """Seed four Shares whose bearers are known sentinels, plus the unknown one.

    The seeds are COMMITTED: the HTTP handlers open their own connection through
    `render_share_connection()`, so an uncommitted fixture is invisible to the code
    under test and every path would answer `unknown` while the test still passed.
    """
    if not _DSN:
        pytest.skip("TEST_POSTGRES_DSN not set; a skip is not a pass")

    from datetime import datetime, timezone  # noqa: PLC0415

    import psycopg  # noqa: PLC0415
    from core import render_shares  # noqa: PLC0415
    from core.render_shares_api import render_share_routes  # noqa: PLC0415
    from starlette.applications import Starlette  # noqa: PLC0415
    from starlette.testclient import TestClient  # noqa: PLC0415
    from ulid import ULID  # noqa: PLC0415

    from tests.integration.test_render_shares_postgres import Chain  # noqa: PLC0415

    previous_db = os.environ.get("PLATFORM_DB_URL")
    previous_limit = os.environ.get("TOOROW_RENDER_SHARE_RATE_LIMIT")
    os.environ["PLATFORM_DB_URL"] = _DSN
    render_shares.reset_rate_limits()

    conn = psycopg.connect(_DSN)
    bearers: dict[str, str] = {}
    seeded_share_ids: list[str] = []
    run = str(ULID())
    try:
        chain = Chain(conn).build()

        def seed(tag: str, *, state: str, expires_sql: str) -> None:
            bearer = _sentinel(tag, run)
            bearers[tag] = bearer
            share_id = "rsh_" + str(ULID())
            seeded_share_ids.append(share_id)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.render_shares (id, org_id, project_id, render_id, "
                    "bearer_hash, state, expires_at, created_at, created_by, revoked_at, "
                    "revoked_by, revoke_reason_code, created_operation_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, " + expires_sql
                    + ", NOW() - interval '1 day', 'owner@example.com', %s, %s, %s, %s)",
                    (
                        share_id,
                        chain.org_id,
                        chain.project_id,
                        chain.render_id,
                        render_shares.bearer_hash(bearer),
                        state,
                        datetime.now(timezone.utc) if state == "revoked" else None,
                        "owner@example.com" if state == "revoked" else None,
                        "revoked_by_operator" if state == "revoked" else None,
                        "op_" + str(ULID()),
                    ),
                )

        seed("SUCCESS", state="active", expires_sql="NOW() + interval '2 days'")
        seed("EXPIRED", state="active", expires_sql="NOW() - interval '1 hour'")
        seed("REVOKED", state="revoked", expires_sql="NOW() + interval '2 days'")
        seed("LIMITED", state="active", expires_sql="NOW() + interval '2 days'")
        # Never seeded: it must answer the same envelope as all of the above.
        bearers["UNKNOWN"] = _sentinel("UNKNOWN", run)
        conn.commit()

        client = TestClient(
            Starlette(routes=render_share_routes), raise_server_exceptions=False
        )
        yield {
            "client": client,
            "bearers": bearers,
            "conn": conn,
            "chain": chain,
            "capture": capture,
        }
    finally:
        conn.rollback()
        conn.close()
        render_shares.reset_rate_limits()
        if previous_db is None:
            os.environ.pop("PLATFORM_DB_URL", None)
        else:
            os.environ["PLATFORM_DB_URL"] = previous_db
        if previous_limit is None:
            os.environ.pop("TOOROW_RENDER_SHARE_RATE_LIMIT", None)
        else:
            os.environ["TOOROW_RENDER_SHARE_RATE_LIMIT"] = previous_limit


def _access_events(conn, project_id: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, share_id, org_id, project_id, event, outcome, reason_code, "
            "client_ip_hash, client_class FROM app.render_share_access_events "
            "WHERE project_id = %s OR project_id IS NULL ORDER BY occurred_at",
            (project_id,),
        )
        return cur.fetchall()


def test_the_five_real_outcome_paths_leak_no_bearer_anywhere(driven):
    """Success, denial, expiry, revoked and rate-limited -- driven, then swept.

    Each path is asserted to have HAPPENED (its status code, and the reason code it
    appended) before the sweep runs, because a sweep over paths that silently did
    not execute is the same empty guard in a different costume.
    """
    from core import render_shares  # noqa: PLC0415

    client = driven["client"]
    bearers = driven["bearers"]
    conn = driven["conn"]
    chain = driven["chain"]
    outcomes: dict[str, int] = {}

    # 1 -- SUCCESS. The bearer is consumed and a narrow session cookie is minted.
    response = client.post("/api/render-shares/exchange", json={"bearer": bearers["SUCCESS"]})
    outcomes["success"] = response.status_code
    assert response.status_code == 200, response.text
    assert "__session=" in response.headers["set-cookie"]
    assert bearers["SUCCESS"] not in response.text
    assert bearers["SUCCESS"] not in response.headers["set-cookie"]

    # 1b -- the granted read past the exchange, so the session paths are swept too.
    read = client.get("/api/render-shares/session/render")
    assert read.status_code in (200, 401), read.text

    # 2 -- DENIAL: a bearer that was never stored.
    response = client.post("/api/render-shares/exchange", json={"bearer": bearers["UNKNOWN"]})
    outcomes["denied"] = response.status_code

    # 3 -- EXPIRY.
    response = client.post("/api/render-shares/exchange", json={"bearer": bearers["EXPIRED"]})
    outcomes["expired"] = response.status_code

    # 4 -- REVOKED.
    response = client.post("/api/render-shares/exchange", json={"bearer": bearers["REVOKED"]})
    outcomes["revoked"] = response.status_code

    # 5 -- RATE LIMITED. One request per window, so the second is refused BEFORE any
    # lookup -- precisely the path where a naive implementation logs the input it
    # just rejected.
    os.environ["TOOROW_RENDER_SHARE_RATE_LIMIT"] = "1"
    render_shares.reset_rate_limits()
    first = client.post("/api/render-shares/exchange", json={"bearer": bearers["LIMITED"]})
    second = client.post("/api/render-shares/exchange", json={"bearer": bearers["LIMITED"]})
    outcomes["rate_limited"] = second.status_code
    assert first.status_code in (200, 404), first.text
    assert second.headers.get("Retry-After"), "a 429 without Retry-After is not a limit"

    # Every one of the five paths reached the database and left its own evidence.
    rows = _access_events(conn, chain.project_id)
    reasons = {row[6] for row in rows}
    events = {row[4] for row in rows}
    assert "granted" in reasons, reasons
    assert "bearer_unknown" in reasons, reasons
    assert "share_expired" in reasons, reasons
    assert "share_revoked" in reasons, reasons
    assert "rate_limited" in events, events

    # THE SWEEP. Every sentinel against every captured record -- at DEBUG, on the
    # ROOT logger, so it covers the ASGI access log and any module that logs.
    _assert_clean(driven["capture"], *bearers.values())

    # And against the persisted evidence: an access-event row is a durable log.
    blob = " ".join(repr(row) for row in rows)
    for bearer in bearers.values():
        assert bearer not in blob, "an access-event row carries bearer material"
        assert bearer[:8] not in blob, "an access-event row carries a bearer prefix"

    # The session value the exchange minted is not in the logs or the evidence.
    session_cookie = client.cookies.get("__session")
    assert session_cookie, "the success path minted no session, so nothing was swept"
    assert session_cookie not in blob and session_cookie[:8] not in blob
    assert session_cookie not in driven["capture"].text
    assert session_cookie[:8] not in driven["capture"].text

    assert outcomes == {
        "success": 200,
        "denied": 404,
        "expired": 404,
        "revoked": 404,
        "rate_limited": 429,
    }, outcomes
