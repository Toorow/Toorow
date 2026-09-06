"""Rendre une ligne de base de donnees en JSON sans casser sur un `datetime`.

POURQUOI CE MODULE EXISTE, et pourquoi il n'est pas un helper de confort.

Mesure du 2026-08-03, en se servant du Context Hub par son API. La meme faute
s'est presentee TROIS fois, dans trois fichiers, avec trois symptomes qui
n'avaient l'air d'avoir aucun rapport :

  1. `core/audit.py` -- `json.dumps(metadata)` sans `default=`. L'ecriture metier
     PASSE, puis l'audit leve. Le handler rend `db_error`, l'appelant reessaie,
     et chaque reprise DUPLIQUE. C'est ainsi que 108 Knowledge orphelines sont
     nees d'un script lance trois fois.
  2. `core/business_taxonomy_api.py` -- `JSONResponse` sur une ligne qui porte
     `created_at`. Les ECRITURES ont ete couvertes en premier ; la LECTURE est
     restee cassee, ce qui est pire : `GET /api/context/business-taxonomy`
     rendait 500 alors que trois Business Domains existaient en base. Invisibles
     de la console comme d'un agent.
  3. `core/context_api.py` -- `GET /api/context/graph`, la route qui compose le
     graphe entier. Meme exception, meme 500. Le graphe paraissait vide pendant
     que la base portait 3 domaines, 9 Skills, 36 Knowledge et 45 liens.

Aucune de ces trois n'a de test qui la couvre, et aucune n'est diagnosticable
depuis le message rendu : `db_error` envoie chercher du cote de Postgres, qui
n'a rien fait de mal.

CE QUE CE MODULE NE FAIT PAS. Il ne rend pas n'importe quel objet serialisable.
Seuls les types temporels sont convertis, en ISO 8601 -- la forme que le reste de
l'API utilise deja. Tout autre type non serialisable leve comme avant : un
`Decimal` ou un `UUID` qui remonte jusqu'ici est une couche de mapping qui
manque, pas un probleme d'encodage, et l'etouffer le rendrait indetectable.
"""

from __future__ import annotations

import datetime as _dt
import json as _json
from typing import Any

from starlette.responses import JSONResponse


def temporal_default(value: Any) -> str:
    """Le seul elargissement autorise : les types temporels, en ISO 8601."""
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def json_scalar(value: Any) -> Any:
    """One column value, JSON-ready: a date-like becomes ISO 8601, the rest passes.

    AI-219, generalising the rule story 63.1 wrote into
    `datastream_publication._row_to_execution`: THE TYPE DECIDES, NEVER A LIST OF
    NAMES. Ten serialisers across seven modules each carried their own
    ``if col in ("created_at", "updated_at")``, and every one of them was a mute
    500 waiting for the next migration -- `JSONResponse` serialises inside
    ``render()``, which Starlette calls AFTER the handler returned, so the
    ``TypeError`` lands OUTSIDE the ``try/except`` that was written to report it.
    A list of column names cannot be kept in step with the schema: nothing tells
    its author that a column landed. So the lists are gone.

    ``bool`` is a subclass of ``int`` and ``datetime`` of ``date``; neither trips
    this, because the only test made is on the temporal family.
    """
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    return value


def row_to_json(columns: Any, row: Any) -> dict[str, Any]:
    """Zip column names with one row, every temporal value already ISO 8601.

    Accepts either a list of names or a DB-API ``cursor.description``, because
    the callers hold one or the other and a second helper would be a second
    place to forget. Surplus row values with no name are dropped, as ``zip``
    does -- a mismatch is a bug in the SELECT, not something to paper over here.
    """
    # A name is a `str`; ANYTHING ELSE is a description entry, and DB-API says
    # its first element is the name. Testing for `str` rather than for `tuple`
    # is what makes this work with psycopg's `Column` objects -- which are not
    # tuples -- without a second branch per driver.
    names = [item if isinstance(item, str) else item[0] for item in columns]
    return {name: json_scalar(value) for name, value in zip(names, row)}


class RowJSON(JSONResponse):
    """A utiliser des qu'une reponse porte une ligne venue de la base.

    Les corps d'erreur litteraux peuvent rester sur `JSONResponse` : aucun
    `datetime` ne peut y apparaitre.
    """

    def render(self, content: Any) -> bytes:
        return _json.dumps(content, ensure_ascii=False, default=temporal_default).encode("utf-8")
