"""The silent-fallback detector must read what the code DOES, not what it says.

Story 48.3's FX-at-read repair deletes the parity conversion — the one that turned a
missing rate into 1.0 and summed a dollar into a euro total with nothing recorded.
Its macros and modules explain that in prose, quoting the expression they removed,
and the detector matched raw file text: five findings, every one of them a comment
describing the defect's removal. Prose about a defect is not the defect, and a gate
that cannot tell them apart teaches people to stop reading it.

The masking has to be exact in BOTH directions, which is what these cases pin. The
dangerous shape is SQL written as a triple-quoted Python string, so triple quotes
alone cannot mean "prose": only genuine docstrings are masked, identified through
the AST rather than by quoting style.
"""

from __future__ import annotations

import io
import re
import sys
import types
from pathlib import Path

import pytest

PATTERN = r"COALESCE\(\s*(fx_rate|rate|exchange_rate)\s*,\s*1(\.0+)?\s*\)"
SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "finished_work_audit.py"

# The expression under test is COMPOSED, never written out. Spelling it literally
# would put a real parity fallback in a string literal of this file — and the
# detector would be right to fire on it, because a string literal is exactly where
# SQL lives. A test that had to be added to an exemption list would be testing the
# exemption instead of the rule.
FALLBACK = "COALESCE(" + "fx_rate, 1.0)"
FALLBACK_RATE = "COALESCE(" + "rate, 1.0)"
Q3 = '"' * 3


def _audit_module() -> types.ModuleType:
    """Load the audit's helpers without running its CLI."""
    source = io.open(SCRIPT, encoding="utf-8").read().split("def main(")[0]
    module = types.ModuleType("finished_work_audit_probe")
    module.__file__ = str(SCRIPT)
    sys.modules["finished_work_audit_probe"] = module
    exec(compile(source, str(SCRIPT), "exec"), module.__dict__)  # noqa: S102
    return module


CASES = [
    # (filename, text, should_fire, label)
    ("model.sql", f"SELECT {FALLBACK} AS eur FROM t", True, "executable SQL"),
    ("model.sql", f"-- we removed {FALLBACK} here", False, "SQL line comment"),
    ("model.sql", "{#- was " + FALLBACK + " -#}", False, "jinja comment"),
    ("model.sql", f"/* {FALLBACK} */", False, "SQL block comment"),
    ("m.py", "q = " + Q3 + f"SELECT {FALLBACK}" + Q3, True, "SQL in a triple-quoted string"),
    ("m.py", "def f():\n    " + Q3 + f"{FALLBACK} is gone." + Q3 + "\n    return 1",
     False, "docstring"),
    ("m.py", f"# {FALLBACK} removed", False, "python comment"),
    ("m.py", f'x = "{FALLBACK}"', True, "SQL in a plain string"),
    # TSX had NO branch in the masker until 2026-09-02, so every scan of a
    # component read its comments as code -- `DataTree.tsx:6` explains that
    # `application.css` is NOT loaded and was reported as loading it. The last
    # case is why the branch is a scanner and not a regex: `//` opens the middle
    # of every URL, and blanking from it would hide a real call.
    ("c.tsx", f"// removed {FALLBACK} here", False, "TSX line comment"),
    ("c.tsx", f"/* was {FALLBACK} */", False, "TSX block comment"),
    ("c.tsx", f' * quoting {FALLBACK} inside a doc block', True, "not a comment on its own"),
    ("c.tsx", f'const q = "{FALLBACK}"', True, "SQL in a TSX string"),
    ("c.tsx", f'fetch("https://h/x?q={FALLBACK}")', True, "a URL is not a comment"),
    ("c.tsx", f'const s = "// {FALLBACK}"', True, "a comment marker inside a string"),
]


@pytest.mark.parametrize(("filename", "text", "should_fire", "label"), CASES)
def test_masking_distinguishes_code_from_prose(filename, text, should_fire, label):
    module = _audit_module()
    masked = module._code_only(text, Path(filename))
    fires = bool(re.search(PATTERN, masked, re.I))
    assert fires is should_fire, (
        f"{label}: the detector {'missed' if should_fire else 'invented'} a silent "
        f"fallback. Masked text was {masked!r}"
    )


def test_masking_preserves_line_numbers():
    """A finding's line number is how someone finds it; blanking must not shift it."""
    module = _audit_module()
    text = f"-- {FALLBACK_RATE}\nSELECT {FALLBACK}\n"
    masked = module._code_only(text, Path("model.sql"))
    assert masked.count("\n") == text.count("\n")
    match = re.search(PATTERN, masked, re.I)
    assert match is not None
    assert masked[: match.start()].count("\n") + 1 == 2, "the real one is on line 2"
