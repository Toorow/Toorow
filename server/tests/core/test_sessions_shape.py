"""`scripts/check_sessions_shape.py` refuses a SESSIONS.md that became a journal.

Same reasoning as `test_sprint_status_shape.py`: a script nobody runs is a wish,
a test that imports it runs in every suite.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
shape = pytest.importorskip("check_sessions_shape")

_TODAY = dt.date(2026, 9, 6)

_CLEAN = """# Sessions en cours

## 2026-09-05 — Epic 76 : consistance écran par écran

Fichiers tenus : `ui/admin/src/ui/*`.

## Ce que je fais en arrivant

1. Lire ce fichier.
"""


def test_a_live_file_has_no_finding():
    assert shape.findings(_CLEAN, _TODAY) == []


def test_an_entry_older_than_three_days_is_dead():
    text = _CLEAN.replace("## 2026-09-05", "## 2026-09-01")
    found = shape.findings(text, _TODAY)
    assert len(found) == 1
    assert "5 days old" in found[0] and "remove the entry" in found[0]


def test_a_quoted_entry_and_a_table_row_are_dated_entries_too():
    text = _CLEAN + "\n> **2026-08-22 — session X. EN COURS.**\n\n| 2026-08-04 | 48-5 | `a.py` |\n"
    found = shape.findings(text, _TODAY)
    assert len(found) == 2


def test_a_finished_entry_coordinates_nothing():
    text = _CLEAN.replace("Epic 76 : consistance", "session X. TERMINÉE, fichiers libérés —")
    found = shape.findings(text, _TODAY)
    assert len(found) == 1 and "finished" in found[0]


def test_the_size_ceiling_fires_last():
    text = _CLEAN + "x" * shape.MAX_BYTES
    found = shape.findings(text, _TODAY)
    assert len(found) == 1 and "ceiling" in found[0]


def test_the_real_file_is_live_today():
    path = _REPO_ROOT / "SESSIONS.md"
    if not path.is_file():
        pytest.skip("no SESSIONS.md here")
    found = shape.findings(path.read_text(encoding="utf-8"), dt.date.today())
    assert found == [], "\n".join(found)
