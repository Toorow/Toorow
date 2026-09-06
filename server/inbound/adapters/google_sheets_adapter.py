"""The Google Sheets read adapter `google_sheets_sync` has always accepted.

`run_sync` takes a `sheets_adapter` callable and raises `NotImplementedError`
when it is None. Both live callers used to pass None -- the scheduler dispatch
hook and the admin sync-now route -- so every Google Sheets sync raised before
reading a cell. The channel had never fetched a row, and the reason was not
that the reader was missing: `modules/google-sheets/connector._fetch_sheet_values`
has been there since Story 15.6, and it already returns exactly the
`list[list[str]]` shape `run_sync` expects. What was missing is the six lines
between them. Both callers now resolve this factory through the
`managed_feed_values_adapter_factory` capability of the inbound seam (AD-2).

The gap is a signature, not a capability. `run_sync` calls its adapter with
`(connection_id, spreadsheet_id, sheet_range)` because core must not know how a
token is obtained; the module's fetch takes `(token, spreadsheet_id, sheet_range)`
because it must not know how a connection is stored. This closure is the one
place allowed to know both, which is why it lives in the adapters layer and not
in `core` -- AD-2 keeps connector knowledge out of the kernel, and
`google_sheets_sync` goes as far as matching `RateLimitError` by class NAME to
avoid importing from `server/modules`.

It deliberately catches nothing. `run_sync` already classifies adapter failures
into safe-fail codes and re-raises rate limits so the worker breaker sees them;
a `try` here would swallow the distinction it depends on.
"""

from __future__ import annotations

from typing import Callable


def google_sheets_values_adapter() -> Callable[[str, str, str], list[list[str]]]:
    """Return the callable `run_sync(sheets_adapter=...)` expects.

    Imports are deferred to call time so that registering the adapter never
    drags the Sheets module, `httpx` or the token service into a process that
    only needed the scheduler.
    """

    def fetch(connection_id: str, spreadsheet_id: str, sheet_range: str) -> list[list[str]]:
        from core.nango_client import get_fresh_token  # noqa: PLC0415

        # AD-21: a Google-stack connection resolves through `auth_path` inside
        # `get_fresh_token`; the caller never picks the token route.
        token = get_fresh_token(connection_id)

        from importlib import import_module  # noqa: PLC0415

        # The module directory is `google-sheets`, which is not an identifier, so
        # it cannot be a plain `from ... import`.
        connector = import_module("modules.google-sheets.connector")
        # AD-3: the token exists only as an argument here and only reaches an
        # Authorization header inside the module. It is never logged or returned.
        return connector._fetch_sheet_values(token, spreadsheet_id, sheet_range)

    return fetch
