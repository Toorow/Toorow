"""The ONE English assertion a runtime test uses -- story 59.5, arbitrage 5.

TWO HALVES, AND NEITHER COVERS THE OTHER. `tests/test_infra_alerts.py` reads the
firing CALL SITES from the source tree: it sees every check, including
`_check_volume`, which no test has a fixture for -- and it is blind to a sentence
composed at runtime out of pieces it cannot follow. This module is the other half:
it reads the message a check ACTUALLY produced, and it is blind to the checks a
test never runs. A guard that had only one of the two halves would be reported as
"the messages are English" while being nothing of the sort.

There is ONE marker list, here, and `test_infra_alerts.py` imports it. Two copies
of a word list is how the static half and the runtime half start disagreeing about
what French looks like.

WHAT THIS IS NOT. It is not a language detector. It is a list of French words with
no English homograph, each of which appeared in a message this repository actually
shipped or in the obvious next one. A French sentence built entirely out of words
absent from it passes -- which is why the static half exists too.
"""

from __future__ import annotations

import re

#: French markers with no English homograph. Every one of them appeared in a
#: message this repository actually shipped, or in the obvious next one.
FRENCH_MARKERS = frozenset(
    {
        "le", "la", "les", "des", "du", "une", "aux", "pour", "dans", "avec",
        "sans", "aucun", "aucune", "sur", "par", "est", "sont", "vers", "apres",
        "avant", "depuis", "entre", "ligne", "lignes", "colonne", "colonnes",
        "valeur", "valeurs", "seuil", "seuils", "doublon", "doublons",
        "detecte", "detectes", "detectee", "detectees", "modifie", "modifiee",
        "rejete", "rejetes", "rejetee", "rejetees", "anormal", "anormale",
        "resolue", "resolu", "correspondent", "candidat", "candidats",
        "distincte", "distinctes", "geographie", "echec", "erreur", "fichier",
        "jour", "jours", "nuit", "heure", "heures", "manquant", "manquante",
        "introuvable", "impossible", "aucunes", "toutes", "tous",
        "ponctualite", "coherence", "moniteur", "moniteurs",
    }
)

_WORD = re.compile(r"[A-Za-z]+")


def french_words(text: str) -> list[str]:
    """The markers *text* carries, sorted. Empty means "nothing was recognised"."""
    words = {word.lower() for word in _WORD.findall(str(text or ""))}
    return sorted(words & FRENCH_MARKERS)


def assert_english(text: str, *, where: str) -> None:
    """Refuse a produced sentence that carries a French marker.

    *where* names what produced it -- an alert type, a monitor label, a column --
    because the failure a person reads has to say which writer to go and fix.
    """
    offenders = french_words(text)
    assert not offenders, f"non-English text from {where}: {offenders} in {text!r}"


def assert_english_firings(rows, *, where: str) -> None:
    """Every `(label, message)` pair a check produced, at runtime.

    Takes the rows as read from `app.alert_firings` -- `(type, message)` -- so a
    bridge test can hand it the table it just measured rather than rebuilding a
    sentence the check never wrote.
    """
    for alert_type, message in rows:
        assert_english(message, where=f"{where} {alert_type}")
