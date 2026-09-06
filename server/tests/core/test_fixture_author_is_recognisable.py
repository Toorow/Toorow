"""L'org de fixture ne doit jamais se faire passer pour la plateforme (AI-183).

CE QUI EST ARRIVÉ. Une ligne de `app.org_members` en **production** portait
`identity='system'`, `role='owner'`, `status='active'`. Lue en base, elle dit :
une identité non-humaine possède une organisation cliente. Elle a été rapportée
comme telle, et il a fallu remonter l'audit puis le code pour découvrir que
c'était `conftest.py` — la fixture partagée, arrivée là avant que le garde-fou
`_refusal_reason` n'existe.

Le défaut n'était pas la fixture. C'était **son nom** : `system` est exactement
ce qu'écrirait un acteur plateforme légitime, donc la ligne ne pouvait pas être
disculpée d'un coup d'œil.

CE QUE CE FICHIER TIENT. Deux propriétés qui se contredisent en apparence, et
dont la contradiction est précisément ce qui s'était mal résolu :

1. l'auteur de la fixture se reconnaît comme une fixture ;
2. et il n'est PAS balayé en fin de session — d'autres sessions rattachent
   leurs projets à cette org, la détruire ferait tomber leur travail.

Aucun Postgres n'est requis : ce sont des propriétés de la constante et du SQL,
pas de la base. C'est voulu — un garde-fou qui ne tourne que derrière un gate
pg-gated ne tourne pas.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.conftest import _TEST_AUTHORS, FIXTURE_AUTHOR, TEST_ORG_ID

_CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"

#: Ce qu'un acteur de la plateforme écrit. Un nom de fixture n'a rien à y faire.
_PLATFORM_LOOKING = frozenset({"system", "anonymous", "platform", "owner", "admin", "root"})


def test_fixture_author_cannot_be_mistaken_for_the_platform() -> None:
    assert FIXTURE_AUTHOR.lower() not in _PLATFORM_LOOKING, (
        f"{FIXTURE_AUTHOR!r} est indistinguable d'un acteur plateforme. "
        "C'est ce qui a fait lire la fixture comme une faille (AI-183)."
    )
    # Se reconnaître ne suffit pas si personne ne peut le chercher : le mot doit
    # y être, pour qu'un `grep` sur une base inconnue rende la réponse.
    assert "fixture" in FIXTURE_AUTHOR.lower()


def test_the_fixture_org_is_not_swept_away_under_other_sessions() -> None:
    # L'autre moitié de la contrainte. Si l'auteur entrait dans `_TEST_AUTHORS`,
    # le balayage de fin de session supprimerait l'org que d'autres sessions
    # utilisent -- exactement la destruction que CLAUDE.md §8 interdit.
    assert FIXTURE_AUTHOR not in _TEST_AUTHORS


def test_conftest_writes_the_fixture_with_that_author_and_nothing_else() -> None:
    """Le SQL de la fixture, lu dans le fichier : aucun `system` en dur ne revient.

    Une constante que le SQL n'utilise pas ne protège rien. Ce test regarde les
    deux INSERT eux-mêmes plutôt que de faire confiance à la constante.
    """
    source = _CONFTEST.read_text(encoding="utf-8")

    org_insert = re.search(r"INSERT INTO app\.organizations.*?\)\s*,", source, re.S)
    member_insert = re.search(r"INSERT INTO app\.org_members.*?\)\s*,", source, re.S)
    assert org_insert and member_insert, "les deux INSERT de la fixture ont bougé"

    for statement in (org_insert.group(0), member_insert.group(0)):
        assert "'system'" not in statement, (
            "la fixture réécrit `system` en dur : c'est le nom qui a coûté AI-183"
        )

    # Et la reprise des bases qui portent encore l'ancien nom doit rester là,
    # sinon une base existante garde une ligne `system` que plus rien ne corrige.
    assert "UPDATE app.org_members SET identity" in source
    assert "AND identity = 'system'" in source
    assert TEST_ORG_ID == "org_test_fixture"
