"""No undefined name anywhere in the server tree -- ruff F821, run as a test.

AI-321 (2026-08-28). `make lint` exists and nobody runs it before landing a
story: story 67.23 (2026-08-22) left two references to an undefined `body` in
`datastream_preconfiguration_api.freeze_and_persist_final_review`, ruff F821
flagged both the day they were written, and they reached production, where
every final review of a file-source Datastream answered a 503 for six days.

A lint that lives outside the gate is a lint nobody runs. This test is the
gate: the whole of `server/core`, `server/inbound` and `server/modules` must
carry ZERO undefined names. It is deliberately narrow -- one rule, the one
that is a crash in waiting -- so it never becomes a style argument.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[2]
_TREES = ("core", "inbound", "modules")


def test_no_undefined_name_in_the_server_tree() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "F821", "--no-fix", *_TREES],
        cwd=_SERVER,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        "undefined names (ruff F821) in the server tree -- each one is a NameError "
        "the first caller will hit in production:\n" + completed.stdout + completed.stderr
    )
