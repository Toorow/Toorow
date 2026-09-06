"""Une reponse qui porte une ligne de base ne passe jamais par `JSONResponse`.

POURQUOI CETTE PORTE EXISTE. Le 2026-08-03, la meme faute a ete trouvee TROIS
fois en se servant du Context Hub par son API -- jamais par un test :

  * `POST /api/context/business-domains` : l'ecriture PASSE, la reponse casse.
    L'appelant reessaie et duplique (108 Knowledge orphelines).
  * `GET /api/context/business-taxonomy` : 500 alors que trois Business Domains
    existaient en base -- donc invisibles de la console comme d'un agent.
  * `GET /api/context/graph` : 500, donc le graphe paraissait VIDE pendant que la
    base portait 3 domaines, 9 Skills, 36 Knowledge et 45 liens.

La cause est toujours la meme : `JSONResponse` ne sait pas serialiser un
`datetime`, et le handler rattrape l'exception en `db_error`, un message qui
envoie chercher du cote de Postgres.

CE QUE LA PORTE VERIFIE, ET POURQUOI ELLE EST MECANIQUE PLUTOT QUE FONCTIONNELLE.
Une porte fonctionnelle exigerait une base ; celle-ci lit l'AST et n'exige rien,
donc elle tourne partout. La regle est nette : dans ces modules, un
`return JSONResponse(...)` n'est licite que sur un CORPS D'ERREUR -- un
dictionnaire litteral portant une cle `code`. Tout le reste vient de la base et
doit passer par `RowJSON`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[2] / "core"

#: Les modules dont CHAQUE reponse non-erreur porte des lignes de base.
GUARDED = ["business_taxonomy_api.py", "context_api.py"]


def _is_declared_constant(node: ast.expr) -> bool:
    """Un nom en SCREAMING_SNAKE_CASE, nu ou pointe : une constante du CODE.

    AJOUTE LE 2026-08-25, et la regle enoncee ne bouge pas d'un pouce : le critere
    reste « aucune valeur ne vient de la base ». Une constante de module est
    ecrite en dur par definition -- elle est juste ecrite ailleurs que sur la
    ligne du `JSONResponse`, ce que la coupure des ecrivains herites impose : le
    refus des quatre portes d'identite du Context Hub porte UNE phrase, declaree
    une seule fois dans `core/business_taxonomy.py`, pour que le 409 de l'API et
    l'exception que recoit un appelant direct ne puissent pas diverger.

    La casse est le critere entier, et c'est ce qui garde la porte armee : une
    ligne de base arrive dans une variable ordinaire (`row`, `answer`, `payload`)
    -- le controle negatif ci-dessous le prouve encore.
    """
    if isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute):
        name = node.attr
    else:
        return False
    return name.isupper() and name.strip("_") != ""


def _is_literal(node: ast.expr) -> bool:
    """Un corps ecrit ENTIEREMENT en dur : aucune valeur ne vient de la base.

    C'est le seul critere qui tienne. « Un dictionnaire dont la cle est `code` »
    a d'abord ete essaye et rejetait `{"status": "requested"}`, un corps pourtant
    litteral -- une porte qui accuse a tort finit desarmee.
    """
    if isinstance(node, ast.Constant):
        return True
    if _is_declared_constant(node):
        return True
    if isinstance(node, ast.Dict):
        return all(_is_literal(value) for value in node.values)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_literal(element) for element in node.elts)
    if isinstance(node, ast.JoinedStr):  # f-string : du texte, jamais une ligne
        return True
    if isinstance(node, ast.Call):
        # `str(exc)` et compagnie : le resultat est du texte.
        return getattr(node.func, "id", None) in {"str", "repr"}
    return False


def _naked_row_responses(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        name = getattr(callee, "id", None) or getattr(callee, "attr", None)
        if name != "JSONResponse" or not node.args:
            continue
        if _is_literal(node.args[0]):
            continue
        offenders.append((node.lineno, ast.unparse(node.args[0])[:80]))
    return offenders


@pytest.mark.parametrize("module", GUARDED)
def test_no_database_row_is_rendered_by_JSONResponse(module: str) -> None:
    offenders = _naked_row_responses((CORE / module).read_text(encoding="utf-8"))
    assert not offenders, (
        f"{module} rend des donnees non litterales via JSONResponse -- "
        "un `datetime` y ferait 500 apres une ecriture reussie. Utiliser RowJSON "
        "(core/row_json.py). Lignes : "
        + ", ".join(f"{line} ({expr})" for line, expr in offenders)
    )


def test_the_guard_can_actually_fail() -> None:
    """Controle negatif : sans lui, une porte verte ne prouverait rien.

    Quatre des cinq fausses alertes du 2026-08-03 venaient d'une sonde muette.
    """
    offenders = _naked_row_responses(
        "from starlette.responses import JSONResponse\n"
        "def h(row):\n"
        "    return JSONResponse(row)\n"
    )
    assert offenders, "la porte ne detecte pas une reponse qui porte une ligne"

    assert not _naked_row_responses(
        "from starlette.responses import JSONResponse\n"
        "def h():\n"
        "    return JSONResponse({'code': 'not_found', 'message': 'x'}, status_code=404)\n"
        "def i():\n"
        "    return JSONResponse({'status': 'requested'}, status_code=201)\n"
    ), "la porte accuse un corps ecrit entierement en dur"

    # Une CONSTANTE declaree -- nue ou pointee -- passe, et une variable
    # ordinaire ne passe toujours pas. Les deux moities dans la meme assertion :
    # elargir sans reverifier le refus est la facon dont une porte se desarme.
    assert not _naked_row_responses(
        "from starlette.responses import JSONResponse\n"
        "from core import business_taxonomy as taxonomy\n"
        "REFUSED = {'code': 'legacy_store_is_read_only'}\n"
        "def h():\n"
        "    return JSONResponse(REFUSED, status_code=409)\n"
        "def i():\n"
        "    return JSONResponse({'message': taxonomy.LEGACY_MESSAGE}, status_code=409)\n"
    ), "la porte accuse une constante declaree"
    assert _naked_row_responses(
        "from starlette.responses import JSONResponse\n"
        "def h(conn):\n"
        "    answer = read_row(conn)\n"
        "    return JSONResponse(answer)\n"
    ), "une variable ordinaire doit rester refusee"
    assert _naked_row_responses(
        "from starlette.responses import JSONResponse\n"
        "def h(row):\n"
        "    return JSONResponse({'domain': row.name})\n"
    ), "un attribut de ligne doit rester refuse"


def test_row_json_renders_datetimes_and_still_refuses_the_rest() -> None:
    import datetime
    import json

    from core.row_json import RowJSON

    moment = datetime.datetime(2026, 8, 3, 19, 5, 10)
    rendered = json.loads(RowJSON({"created_at": moment}).render({"created_at": moment}))
    assert rendered["created_at"] == moment.isoformat()

    # Un type non temporel doit TOUJOURS lever : un `Decimal` qui remonte
    # jusqu'ici est une couche de mapping qui manque, pas un souci d'encodage.
    class Opaque:
        pass

    with pytest.raises(TypeError):
        RowJSON({}).render({"x": Opaque()})
