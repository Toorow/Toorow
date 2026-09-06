"""One JSON response that can carry what the database actually returns.

`starlette.responses.JSONResponse` calls `json.dumps` with no `default`, so a
payload holding a `datetime` -- the ordinary shape of every `timestamptz`
column psycopg hands back -- raises `TypeError` at RENDER time, after the
handler has already succeeded. A seam that maps unknown exceptions to a 503
therefore reports "evidence is unavailable" for a payload it composed
correctly, and no amount of data can change that answer.

That is exactly how `GET .../workbench/overview` failed: `read_tab` returned a
complete overview, `schedule.next_run_at` was a `datetime`, and the tab -- which
hosts the inbound delivery address -- answered 503 forever. Unit tests never saw
it because they assert the dict `read_tab` returns and never render it.

Encoding belongs here, once, and not in each read model: a read model that
pre-stringifies its own timestamps has to remember to do it in every branch,
and the one branch that forgets fails at render again.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from starlette.responses import JSONResponse

# `datetime` subclasses `date`, so it is tested first.
_UTC_SUFFIX = "+00:00"


def json_default(value: Any) -> Any:
    """Encode the non-JSON types a database row legitimately carries."""
    if isinstance(value, datetime):
        return value.isoformat().replace(_UTC_SUFFIX, "Z")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        # A quantity, not a float: exactness matters more than JSON number
        # ergonomics, and every consumer of a money/row-count column parses it.
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError("binary payloads are never serialized to a client")
    if isinstance(value, (set, frozenset)):
        return sorted(str(item) for item in value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class SafeJSONResponse(JSONResponse):
    """`JSONResponse` that renders database-native values instead of failing.

    Starlette's own separators and `allow_nan=False` are preserved: a NaN still
    refuses rather than emitting invalid JSON.
    """

    def render(self, content: Any) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
            default=json_default,
        ).encode("utf-8")
