"""The console navigation registry, read as ONE text -- AD-42/AD-43.

WHY THIS EXISTS. `navigation.ts` used to hold every workspace in a single
313-line array. Since AD-42 each workspace declares its own sections in
`shell/navigation/<workspace>.ts`, and a test that reads the one path finds an
assembly with no content in it.

That is not a failure a reader notices: `navigation.split('key: "analyze"')`
raises, but `"section(...)" in navigation` simply answers False -- **a guard
pinned to a PATH goes quietly true when the code moves.** Measured the day the
registry was split: `object types declared in navigation.ts` fell from 31 to 0
while the audit kept reporting "0 rendered by nothing".

Every server test that reads the registry reads it through here.
"""

from __future__ import annotations

import pathlib

_UI_SHELL = pathlib.Path(__file__).resolve().parents[3] / "ui" / "admin" / "src" / "shell"


def navigation_source(shell: pathlib.Path | None = None) -> str:
    """`navigation.ts` and every workspace file it assembles, joined."""
    base = shell or _UI_SHELL
    parts = [base / "navigation.ts", *sorted((base / "navigation").glob("*.ts"))]
    return chr(10).join(p.read_text(encoding="utf-8") for p in parts if p.exists())
