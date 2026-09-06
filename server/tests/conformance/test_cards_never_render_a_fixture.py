"""A card entrypoint may not reach its fixture (AI-271).

WHY THIS GUARD, AND WHY IT IS PLACED HERE. All nine card entrypoints ended with
the same line -- `render(readInjectedEnvelope<CardEnvelope>() ?? FIXTURE_ENVELOPE)`
-- so a card opened without an envelope rendered its FIXTURE: a complete card,
with numbers, that no server ever produced. The `conversions` card went on
showing a 50 EUR target in green after story 53.5 had removed that target from
the server, which is how the defect was found.

The product rule it breaks is not a preference: "no demonstration content" --
never fabricate a screen's data so it looks like it works. Nine identical lines
are one defect, so the repair and its guard are one too.

WHAT IT ASKS. In `ui/cards/*/src/main.tsx` -- the RENDER PATH, and nothing else
-- no fixture may be imported or named. Tests, stories and the fixture files
themselves are untouched: the fixture is the tests' subject matter, and deleting
it would cost the suite its only envelope.

ITS SCOPE, stated because a ratchet that covers one file hides its neighbours: it
binds the entrypoints only. A fixture reached from `App.tsx` would be the same
defect one file down and this guard would not see it -- so `App.tsx` is checked
too, for the import alone.
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]
CARDS = ROOT / "ui" / "cards"

#: `shell` is the shared chrome package, not a card: it has no entrypoint.
_NOT_A_CARD = {"shell"}


def _card_dirs() -> list[pathlib.Path]:
    return sorted(
        path
        for path in CARDS.iterdir()
        if path.is_dir() and path.name not in _NOT_A_CARD and (path / "src").is_dir()
    )


def test_every_card_has_an_entrypoint_this_guard_can_read() -> None:
    """Calibration: a guard over an empty set proves nothing.

    Nine cards carried the defect. If this count ever drops without the cards
    being removed, the guard has gone blind and says so here first.
    """
    cards = _card_dirs()
    assert len(cards) >= 9, [c.name for c in cards]
    for card in cards:
        assert (card / "src" / "main.tsx").is_file(), card.name


def test_no_card_entrypoint_reaches_its_fixture() -> None:
    offenders: list[str] = []
    for card in _card_dirs():
        entry = card / "src" / "main.tsx"
        for number, line in enumerate(entry.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            # The repair's own comment names the line it replaced, so a comment
            # is not the defect -- executable code that names a fixture is.
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            if "fixture" in stripped.lower() or "FIXTURE" in stripped:
                offenders.append(
                    f"ui/cards/{card.name}/src/main.tsx:{number}: "
                    f"the render path names a fixture -- {stripped}"
                )
    assert not offenders, "\n".join(offenders)


def test_no_card_body_imports_its_fixture() -> None:
    """The same rule one file down, where the render path actually continues."""
    offenders: list[str] = []
    for card in _card_dirs():
        body = card / "src" / "App.tsx"
        if not body.is_file():
            continue
        for number, line in enumerate(body.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("import") and "fixture" in stripped.lower():
                offenders.append(
                    f"ui/cards/{card.name}/src/App.tsx:{number}: "
                    f"the card body imports a fixture -- {stripped}"
                )
    assert not offenders, "\n".join(offenders)
