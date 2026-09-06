"""Every label a manifest ships is in English -- swept across all 39 modules.

WHY THIS GUARD EXISTS, AND WHY IT DID NOT BEFORE.

`report_profiles[].display_name` was dead. `source_capabilities.reports[]`
carries a `display_name` on 2 entries out of 133, and the two projections the
Datastream wizard reads took it from THERE -- so 131 report families reached the
screen as a technical identifier, and nobody could see what the profile actually
said. Thirteen of those profile labels were in French (`gsc` 10, `hubspot` 2,
`generic` 1) and it cost nothing, because nothing rendered them.

Story 57.6 joined `report_profiles[].display_name` into both projections. From
that commit on, those thirteen labels appear on the operator's cards. A field
that was not broken but DEAD becomes load-bearing, and the story that makes it
carry weight is the one that has to close it.

WHY A GUARD RATHER THAN THIRTEEN FIXES. The thirteen are corrected in the same
commit. Without this test the next connector added reopens the class, and this
time nobody notices -- the join makes a French label look exactly as normal as an
English one on the card.

WHAT "ENGLISH" MEANS HERE, and it is deliberately narrow. Two mechanical
refusals, no natural-language guessing:

  1. a NON-ASCII LETTER (`é`, `à`, `ü`). Punctuation is untouched: an em dash in
     `Orders — catalog-driven` is typography, not a language;
  2. a FRENCH FUNCTION WORD as a whole token. Function words are what survive
     every translation of a product label, and they are the only tokens that can
     be listed without judging vocabulary. `date`, `format` and `impressions`
     are spelled the same in both languages and are NOT refused.

A label this test cannot decide is a label it lets through. A guard that refuses
what it cannot judge would be a second authority on the product's wording.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).parents[3]
MODULES_DIR = ROOT / "server" / "modules"

# Whole tokens only, and function words only. Every entry is a word that cannot
# appear in an English product label by accident.
FRENCH_FUNCTION_WORDS = frozenset(
    {
        "par",
        "pour",
        "avec",
        "sans",
        "dans",
        "sur",
        "vers",
        "chez",
        "des",
        "du",
        "de",
        "la",
        "le",
        "les",
        "un",
        "une",
        "et",
        "ou",
        "aux",
        "au",
        "ce",
        "cette",
        "ces",
        "qui",
        "que",
        "quoi",
        "dont",
        "leur",
        "leurs",
        "son",
        "sa",
        "ses",
        "notre",
        "nos",
        "votre",
        "vos",
        "est",
        "sont",
        "etre",
        "avoir",
        "plus",
        "moins",
        "tous",
        "toutes",
        "tout",
        "toute",
        "quotidien",
        "quotidienne",
        "quotidiens",
        "quotidiennes",
        "journalier",
        "journaliere",
        "mensuel",
        "mensuelle",
        "hebdomadaire",
        "jour",
        "jours",
        "mois",
        "annee",
        "semaine",
        "cree",
        "crees",
        "creee",
        "creees",
        "ferme",
        "fermes",
        "fermee",
        "fermees",
        "tableau",
        "mesures",
        "requete",
        "requetes",
        "recherche",
        "onglet",
        "apparence",
        "resultats",
        "images",
        "videos",
        "actualites",
        "champ",
        "champs",
        "compte",
        "comptes",
        "donnees",
        "rapport",
        "rapports",
    }
)

# A token is a maximal run of letters AND digits, deliberately: splitting inside
# `SA360` yields `SA`, which the list below reads as the French possessive and
# refuses a correct English label for. Two kinds of token are then skipped
# outright, because neither can be judged as a word: anything carrying a digit,
# and an all-caps run, which is an acronym (`GAM`, `CPM`, `SA`).
TOKENS = re.compile(r"[A-Za-zÀ-ɏ0-9]+")

# WHERE A LABEL LIVES. Each entry is a path a human reads on a screen; a key
# that is an identifier (`id`, `name`, `field_id`) is deliberately absent --
# identifiers are join keys, not prose, and are never translated.
LABEL_PATHS = (
    ("report_profiles", "display_name"),
    ("report_profiles", "description"),
    ("source_capabilities.reports", "display_name"),
    ("source_capabilities.reports", "description"),
    ("source_capabilities.fields", "description"),
)


def _fold(value: str) -> str:
    """Accents removed, so `requête` and `requete` are the same token to the list."""
    return "".join(
        char
        for char in unicodedata.normalize("NFD", value)
        if unicodedata.category(char) != "Mn"
    )


def _non_english_reason(label: str) -> str | None:
    for char in label:
        if ord(char) > 127 and char.isalpha():
            return f"non-ASCII letter {char!r}"
    hits = sorted(
        {
            _fold(token).lower()
            for token in TOKENS.findall(label)
            if not any(char.isdigit() for char in token)
            and not token.isupper()
            and _fold(token).lower() in FRENCH_FUNCTION_WORDS
        }
    )
    if hits:
        return "French token(s) " + ", ".join(repr(hit) for hit in hits)
    return None


def _entries(manifest: dict, dotted: str) -> list[dict]:
    node: object = manifest
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return []
        node = node.get(part)
    return [item for item in (node or []) if isinstance(item, dict)]


def _labels() -> list[tuple[str, str, str, str]]:
    """(module, where, identifier, label) for every human-read label on disk."""
    found: list[tuple[str, str, str, str]] = []
    for path in sorted(MODULES_DIR.glob("*/manifest.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        module = str(manifest.get("name") or path.parent.name)
        for dotted, key in LABEL_PATHS:
            for entry in _entries(manifest, dotted):
                label = entry.get(key)
                if isinstance(label, str) and label.strip():
                    identifier = str(entry.get("id") or entry.get("field_id") or "?")
                    found.append((module, f"{dotted}.{key}", identifier, label))
    return found


def test_the_sweep_actually_reaches_every_module_and_every_profile():
    """A guard that measures nothing passes for the wrong reason.

    Pinned to the two counts this repository already states elsewhere: 39
    manifests, 140 report profiles. A drop in either means the sweep stopped
    reading, not that the labels got better.
    """
    manifests = sorted(MODULES_DIR.glob("*/manifest.json"))
    assert len(manifests) == 39
    profiles = sum(
        len(json.loads(path.read_text(encoding="utf-8")).get("report_profiles") or [])
        for path in manifests
    )
    # 2026-08-11: youtube-analytics exposes its seven documented breakdowns.
    # 2026-09-01: + competitor_channel_snapshot and channel_video_directory (Competitors outbound).
    assert profiles == 142
    assert len([item for item in _labels() if item[1].endswith("display_name")]) >= profiles


def test_every_manifest_label_a_person_reads_is_in_english():
    offenders = [
        f"{module} | {where} | {identifier} | {label!r} -> {reason}"
        for module, where, identifier, label in _labels()
        if (reason := _non_english_reason(label))
    ]
    assert not offenders, (
        "Manifest labels reach the operator's screen since story 57.6 joined "
        "`report_profiles[].display_name` into the wizard's projections. Every "
        "product label is English:\n" + "\n".join(offenders)
    )


def test_the_guard_refuses_what_it_is_meant_to_refuse():
    """The guard's own proof, so a silent detector is not mistaken for a clean tree."""
    assert _non_english_reason("Performance par page (quotidienne)")
    assert _non_english_reason("Contacts crees par jour (daily)")
    assert _non_english_reason("Recherche de vidéos par page")
    # Typography is not a language: an em dash and an accentless English label
    # both pass, and so do the words the two languages share.
    assert _non_english_reason("Orders — catalog-driven (any field, order grain)") is None
    assert _non_english_reason("Page performance (daily)") is None
    assert _non_english_reason("Impressions by date and format") is None
    # An acronym is not a word: `SA360` and `SA` must not be read as the French
    # possessive `sa`. This is the false positive the first run of this guard
    # produced, and it is pinned so the tokenizer cannot regress into it.
    assert _non_english_reason("Explicit compatible SA360 fields") is None
    assert _non_english_reason("SA campaign spend") is None
