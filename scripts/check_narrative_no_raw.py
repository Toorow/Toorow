#!/usr/bin/env python3
r"""La narration ne LIT aucune donnee. Le garde interdisait de la NOMMER.

CE QU IL ETAIT (Makefile, check-narrative-no-raw, jusqu au 2026-08-20) :

    grep -E "^(import|from)\s+(server\.core\.warehouse|core\.warehouse|warehouse)"
    grep -nE "fact_daily_kpi|raw_gsc|raw_meta|raw_ga4|\.rows\b"

Deux faiblesses symetriques, et ensemble elles annulaient le garde :

  1. TROP LARGE : il interdisait la chaine `fact_daily_kpi` -- or cette chaine
     est la CITATION que AD-1/AD-9 exigent. `_pull_citation` l ecrit dans le
     token `(connector:fact_daily_kpi, pull_id)` qui rend chaque chiffre
     rattachable a sa source (narrative.py:324-335). Le garde rougissait donc
     sur le code actuel, en permanence : il interdisait le geste qu il etait
     cense proteger.
  2. TROP ETROIT : quatre noms de tables en dur. Un `import core.db`, un
     `psycopg`, un `duckdb.connect`, un `SELECT * FROM raw_shopify_orders`
     passaient tous -- c est-a-dire que la VIOLATION REELLE (lire la donnee)
     etait libre, pendant que la citation honnete etait interdite.

CE QU IL EST MAINTENANT. La frontiere d AD-1 n est pas "ne pas nommer", c est
"ne pas LIRE" : narrative.py recoit des blocs resolus en amont (cards.py) et
les met en phrases. Trois regles visent la lecture, aucune la mention :

  A. AUCUN IMPORT D ACCES AUX DONNEES : warehouse, db, psycopg, duckdb,
     bigquery, raw_landing, mirror_sync... Sans acces, pas de lecture -- c est
     cette regle, pas un nom de table, qui porte l invariant.
  B. AUCUN SQL : pas de SELECT / INSERT INTO / DELETE FROM / UPDATE ... SET.
     La narration n a pas de requete a exprimer, sous aucune forme.
  C. AUCUNE REFERENCE A UNE TABLE RAW OU STAGING (`raw_*`, `stg_*`) : ces
     relations n existent pas dans le vocabulaire de la narration, ni en code
     ni en docstring.

CE QU IL AUTORISE EXPLICITEMENT. `fact_daily_kpi` dans un token de citation :
nommer la relation lue est la preuve, pas la lecture. Le jour ou ce nom sort
du token pour devenir une requete, la regle B rougit.

REGLE D -- LA NARRATION N ECRIT AUCUNE PHRASE (ajoutee le 2026-08-25).

  Arbitrage de Jean du 2026-08-22, ecrit dans
  `docs/product-architecture/analyze-and-test.md` : un recit se rend dans la
  LANGUE DU LECTEUR, donc une phrase de recit ecrite en dur -- dans quelque
  langue que ce soit -- est le defaut. L audit du 2026-08-25 a mesure ~77
  phrases en dur et, surtout, AUCUN INSTRUMENT : une clause ratifiee sans
  garde, c est la 78e phrase le lendemain.

  La regle : dans les SITES DECLARES ci-dessous, aucun litteral de chaine ne
  doit se lire comme une phrase. Les mots vivent dans
  `server/core/narrative_phrases.py`, le seul endroit ou une phrase s ecrit ;
  un site d appel compose par CLE.

  Ce qui compte comme une phrase : deux mots de 2 lettres ou plus separes par
  une espace, OU un seul mot portant une lettre accentuee. Les docstrings sont
  ignorees (elles expliquent, elles ne sont rendues a personne), et le
  catalogue lui-meme est evidemment hors regle.

  CE QUE LA REGLE NE VOIT PAS, dit plutot que tu : un mot isole sans accent
  (`gain`, `perte`) ne declenche rien. Ces mots-la ont ete deplaces dans le
  catalogue quand meme -- la regle attrape la phrase, pas le mot.

LA PORTEE DU GARDE, IMPRIMEE A CHAQUE RUN, ET RE-DERIVEE (2026-08-31). Elle
couvre une liste de FICHIERS, et dans chacun elle scanne TOUT -- le module et
chacune de ses fonctions, y compris celles ecrites demain. Il ne dit rien du
reste du depot, et il le dit.

  CE QUI A ETE REPARE. Jusqu au 2026-08-31 la regle D ne visitait que des
  fonctions DECLAREES par leur nom (`{"reports.py": {"build_summary"}}`). Une
  fonction productrice de recit qui n etait pas dans la liste rendait donc
  ZERO offence -- non pas propre : invisible. `reports._state_the_limiting_term`
  et `analyze_render_mcp.build_summary` epelaient des phrases francaises depuis
  des semaines sous un garde vert. C est « un instrument ne mesure pas sa propre
  copie » : le perimetre etait une declaration, pas une derivation, et une
  declaration ne se met pas a jour toute seule.

  CE QUI EST VRAI MAINTENANT. Le perimetre se DERIVE du fichier : absence de la
  liste veut dire SCANNE, jamais invisible. Une fonction ne sort du scan que si
  quelqu un l a NOMMEE dans `EXEMPT_FUNCTIONS` avec sa raison ecrite -- une
  exemption est un acte, tracable et relisible ; l oubli, lui, ne peut plus en
  etre un.

    python scripts/check_narrative_no_raw.py           # rapport
    python scripts/check_narrative_no_raw.py --gate    # non-zero si offence

Le meme code est appele par
`server/tests/conformance/test_core_purity_guards.py` : une regle qui vit dans
deux codes finit par vivre dans deux verites.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NARRATIVE = ROOT / "server" / "core" / "narrative.py"

#: LE CATALOGUE -- le seul fichier ou une phrase s ecrit.
PHRASE_CATALOGUE = "server/core/narrative_phrases.py"

#: LE PERIMETRE DE LA REGLE D : DES FICHIERS, scannes en entier.
#:
#: Chaque fichier est visite au complet -- son module et CHACUNE de ses
#: fonctions, celles d aujourd hui comme celles de demain. Il n y a plus de
#: liste de fonctions a tenir a jour, donc plus rien a oublier d y mettre.
NARRATIVE_FILES: tuple[str, ...] = (
    "server/core/narrative.py",
    "server/core/anomaly_alerts.py",
    "server/core/analyze_render_mcp.py",
    "server/core/summarizer.py",
    "server/core/reports.py",
    "server/core/cards.py",
)

#: LES EXEMPTIONS, NOMMEES UNE PAR UNE, avec leur raison.
#:
#: Le tableau de `analyze-and-test.md:1974-1978` distingue le RECIT (qui va au
#: catalogue) du MESSAGE D OPERATEUR et de la CLE (qui ont le droit d etre en
#: dur). Cette distinction ne se lit pas dans un litteral, donc elle se declare
#: -- mais elle se declare en RETIRANT une fonction du scan, jamais en l y
#: ajoutant : le defaut est « scanne ».
#:
#: La plupart des cas (SQL, log, refus, cle comparee) sont reconnus
#: STRUCTURELLEMENT par `_exempt_nodes` et n ont besoin d aucune ligne ici. Ce
#: dictionnaire est pour ce qu aucune forme ne trahit.
EXEMPT_FUNCTIONS: dict[str, dict[str, str]] = {}

#: Une phrase : deux mots de 2 lettres ou plus, ou un mot accentue. La classe
#: accentuee exclut deliberement U+00D7 (x) et U+00F7 (÷), qui sont des
#: operateurs et vivent dans les formules citees (AD-9), pas dans de la prose.
_PROSE_RE = re.compile(
    r"[A-Za-zÀ-ÖØ-öø-ÿ]{2,}\s+[A-Za-zÀ-ÖØ-öø-ÿ]{2,}|[À-ÖØ-öø-ÿ]"
)

_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(?P<mod>[A-Za-z_][\w.]*)")
_FROM_IMPORT_RE = re.compile(
    r"^\s*from\s+(?P<pkg>core|server\.core)\s+import\s+(?P<names>.+)"
)

#: Sous-modules de `core` qui donnent ACCES a la donnee, pour la forme
#: `from core import warehouse` (le `from` ne capture que le paquet).
_DATA_ACCESS_SUBMODULES = {
    "warehouse",
    "db",
    "raw_landing",
    "warehouse_write",
    "bigquery_raw_writer",
    "mirror_sync",
    "cache_warehouse",
    "collected_mapped_reader",
}

#: Prefixes de modules qui donnent ACCES a la donnee. L interdit porte sur
#: l acces, pas sur un nom : toute couche warehouse/db/brute est visee.
_DATA_ACCESS_PREFIXES = (
    "warehouse",
    "psycopg",
    "duckdb",
    "google.cloud.bigquery",
    "core.warehouse",
    "core.db",
    "core.raw_landing",
    "core.warehouse_write",
    "core.bigquery_raw_writer",
    "core.mirror_sync",
    "core.cache_warehouse",
    "core.collected_mapped_reader",
    "server.core.warehouse",
    "server.core.db",
    "server.core.raw_landing",
    "server.core.mirror_sync",
)

_SQL_RE = re.compile(r"\bSELECT\b|\bINSERT\s+INTO\b|\bDELETE\s+FROM\b|\bUPDATE\s+\w+\s+SET\b")

#: Un FRAGMENT de requete -- ` AND project_id = ?`, ` ORDER BY ABS(z) DESC`.
#:
#: Une requete se compose morceau par morceau, et un morceau ne porte pas
#: forcement son verbe : `_SQL_RE` seul laissait passer la moitie d un WHERE, que
#: la regle D lisait alors comme une phrase de deux mots. Les mots-cles sont
#: cherches EN MAJUSCULES uniquement -- de la prose ne s ecrit pas en capitales,
#: et il en faut DEUX, ou un seul avec un marqueur de parametre : un seuil qu une
#: phrase de recit n atteint pas par accident.
_SQL_KEYWORD_RE = re.compile(
    r"(?<![A-Za-z_])(?:SELECT|FROM|WHERE|AND|OR|NOT|NULL|JOIN|LEFT|INNER|OUTER|ON"
    r"|ORDER BY|GROUP BY|HAVING|LIMIT|OFFSET|DESC|ASC|IS|IN|AS|SET|VALUES|BY)"
    r"(?![A-Za-z_])"
)
_SQL_PLACEHOLDER_RE = re.compile(r"\?|%s|%\(\w+\)s")


def _is_sql_fragment(value: str) -> bool:
    """Une requete, ou un morceau de requete -- jamais une phrase."""
    if _SQL_RE.search(value):
        return True
    keywords = len(set(_SQL_KEYWORD_RE.findall(value)))
    return keywords >= 2 or (keywords == 1 and bool(_SQL_PLACEHOLDER_RE.search(value)))


_RAW_REL_RE = re.compile(r"\b(?:raw|stg)_[a-z0-9_]+\b")


def _is_data_access(module: str) -> bool:
    return any(
        module == prefix or module.startswith(prefix + ".")
        for prefix in _DATA_ACCESS_PREFIXES
    )


def offences(narrative: Path = NARRATIVE) -> list[str]:
    """Toutes les offences AD-1 de narrative.py (vide = propre)."""
    found: list[str] = []
    for lineno, line in enumerate(
        narrative.read_text(encoding="utf-8").splitlines(), 1
    ):
        import_match = _IMPORT_RE.match(line)
        if import_match and _is_data_access(import_match.group("mod")):
            found.append(
                f"{lineno}: import d acces aux donnees ({import_match.group('mod')}) -- "
                f"narrative.py recoit des blocs resolus, il ne lit rien (AD-1): {line.strip()}"
            )
        from_match = _FROM_IMPORT_RE.match(line)
        if from_match:
            imported = {
                part.strip().split(" as ")[0].strip().strip("()")
                for part in from_match.group("names").split(",")
            }
            data_access = imported & _DATA_ACCESS_SUBMODULES
            if data_access:
                found.append(
                    f"{lineno}: import d acces aux donnees "
                    f"({', '.join(sorted(data_access))}) -- narrative.py recoit "
                    f"des blocs resolus, il ne lit rien (AD-1): {line.strip()}"
                )
        if line.lstrip().startswith("#"):
            continue
        if _SQL_RE.search(line):
            found.append(
                f"{lineno}: SQL dans la narration -- elle n a aucune requete a "
                f"exprimer, sous aucune forme (AD-1): {line.strip()}"
            )
        raw_match = _RAW_REL_RE.search(line)
        if raw_match:
            found.append(
                f"{lineno}: reference a une relation brute/staging "
                f"({raw_match.group(0)}) -- ce vocabulaire n existe pas dans la "
                f"narration (AD-1): {line.strip()}"
            )
    return found


# ---------------------------------------------------------------------------
# REGLE D -- aucune phrase ecrite en dur dans un site de recit declare.
# ---------------------------------------------------------------------------


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Les constantes qui sont des docstrings : elles expliquent, on les ignore."""
    marked: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            marked.add(id(body[0].value))
    return marked


def _exempt_nodes(scope: ast.AST) -> set[int]:
    """Les chaines qui ne sont PAS du recit, et pourquoi -- exemption par exemption.

    Le tableau de `analyze-and-test.md:1974-1978` distingue trois objets, et deux
    d entre eux ont le droit d etre en dur : une CLE que du code compare, et un
    MESSAGE D OPERATEUR. Une fonction productrice de recit porte souvent les
    trois ; sans ces exemptions le garde reclamerait de traduire un log.

      * un argument de `logger.*` -- canal d exploitation, jamais rendu a un
        lecteur. Y compris quand le logger a ete lie a un nom (`log = logger.debug
        if ... else logger.warning`), forme que la version du 2026-08-25 ne
        reconnaissait pas : elle exigeait un attribut, et un appel de NOM lui
        echappait ;
      * une chaine construite dans un `raise`, ou passee a un constructeur
        d erreur (`ToolError`, `_tool_error`, `*Refused`) -- un refus est un
        message d operateur, qu il soit leve ici ou retourne pour l etre ailleurs ;
      * une chaine COMPAREE (`x == "..."`, `"..." in x`) -- c est une CLE que du
        code teste, pas une phrase qu un lecteur lit. Le tableau du document de
        surface l autorise en dur, et la deplacer au catalogue casserait la
        comparaison ;
      * une requete SQL, ET SES FRAGMENTS : un f-string dont l ensemble est une
        requete exempte toutes ses parties, parce qu une requete se compose
        morceau par morceau et qu aucun morceau n est une phrase.
    """
    exempt: set[int] = set()

    def _mark(node: ast.AST) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                exempt.add(id(child))

    def _is_error_call(func: ast.AST) -> bool:
        name = (
            func.attr if isinstance(func, ast.Attribute)
            else func.id if isinstance(func, ast.Name)
            else ""
        )
        return name.lower().endswith(("error", "refused", "exception"))

    for node in ast.walk(scope):
        if isinstance(node, (ast.Raise, ast.Compare)):
            _mark(node)
        elif isinstance(node, ast.Call):
            func = node.func
            logger_call = (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in {"logger", "log", "logging"}
            ) or (isinstance(func, ast.Name) and func.id in {"logger", "log", "logging"})
            if logger_call or _is_error_call(func):
                _mark(node)
        elif isinstance(node, ast.JoinedStr):
            joined = "".join(
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            if _is_sql_fragment(joined):
                _mark(node)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _is_sql_fragment(node.value):
                exempt.add(id(node))
    return exempt


def _excluded_nodes(tree: ast.Module, exempt: dict[str, str]) -> set[int]:
    """Les constantes qui vivent dans une fonction NOMMEMENT exemptee.

    LE SENS DE LA LISTE EST INVERSE depuis le 2026-08-31. Elle ne dit plus « ces
    fonctions-la sont regardees » -- ce qui rendait toutes les autres invisibles
    -- mais « ces fonctions-la sont dispensees ». Le module entier est parcouru ;
    seul ce qui est nomme ici en sort, et un nom absent est SCANNE.
    """
    excluded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in exempt:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                excluded.add(id(child))
    return excluded


def prose_offences(
    root: Path = ROOT,
    files: tuple[str, ...] | None = None,
    exempt_functions: dict[str, dict[str, str]] | None = None,
) -> list[str]:
    """Toute phrase ecrite en dur dans un fichier couvert (vide = propre).

    LE PERIMETRE EST DERIVE, PAS DECLARE. Chaque fichier est parcouru EN ENTIER
    -- module et fonctions, celles d aujourd hui comme celles ecrites demain.
    Seule une fonction NOMMEE dans *exempt_functions* sort du scan ; l absence
    d un nom n a plus le pouvoir de rendre du recit invisible.

    *files* et *exempt_functions* n existent que pour qu un test puisse viser un
    arbre jetable : un instrument qui ne peut pas etre mis au rouge n a jamais
    prouve qu il rougit.
    """
    covered = NARRATIVE_FILES if files is None else files
    exemptions = EXEMPT_FUNCTIONS if exempt_functions is None else exempt_functions
    found: list[str] = []
    for relative in sorted(covered):
        path = root / relative
        if not path.exists():
            found.append(f"{relative}: fichier couvert introuvable -- le garde vise le vide")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        file_exemptions = exemptions.get(relative, {})
        present = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        # Une exemption qui ne vise plus rien est une dispense fantome : elle se
        # lit comme un fait sur le fichier alors qu elle ne l est plus.
        for orphan in sorted(set(file_exemptions) - present):
            found.append(
                f"{relative}: exemption sans fonction ({orphan}) -- une dispense "
                f"qui ne vise rien se retire, elle ne se garde pas"
            )
        docstrings = _docstring_nodes(tree)
        excluded = _excluded_nodes(tree, file_exemptions)
        exempt = _exempt_nodes(tree)
        seen: set[tuple[int, str]] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings or id(node) in exempt or id(node) in excluded:
                continue
            if not _PROSE_RE.search(node.value):
                continue
            key = (node.lineno, node.value)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                f"{relative}:{node.lineno}: phrase de recit ecrite en dur -- "
                f"les mots vivent dans {PHRASE_CATALOGUE}, le site compose par "
                f"cle (arbitrage 2026-08-22): {node.value[:70]!r}"
            )
    return found


def _perimeter_lines() -> list[str]:
    """La portee du garde, imprimee : un garde partiel dit son perimetre.

    Et il dit COMMENT il l obtient : ces fichiers-la en entier, toute fonction
    comprise. C est la difference entre un perimetre derive et une liste qu il
    aurait fallu penser a tenir a jour.
    """
    lines = [
        "PORTEE DE LA REGLE D (elle ne dit RIEN du reste du depot) :",
        "  ces fichiers sont scannes EN ENTIER -- module et TOUTE fonction,",
        "  y compris celles ajoutees apres ce run :",
    ]
    lines.extend(f"    {relative}" for relative in sorted(NARRATIVE_FILES))
    exempted = sorted(
        (relative, name, why)
        for relative, entries in EXEMPT_FUNCTIONS.items()
        for name, why in entries.items()
    )
    if exempted:
        lines.append("  dispensees NOMMEMENT (absente de cette liste = scannee) :")
        lines.extend(f"    {relative}::{name} -- {why}" for relative, name, why in exempted)
    else:
        lines.append("  aucune fonction n est dispensee nommement.")
    lines.append(f"  les mots eux-memes vivent dans {PHRASE_CATALOGUE}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--gate", action="store_true", help="exit non-zero si une offence")
    args = parser.parse_args()

    for line in _perimeter_lines():
        print(line)
    print()

    found = offences()
    prose = prose_offences()

    if found:
        print("AD-1 OFFENCES (narrative.py ne LIT aucune donnee):")
        for offence in found:
            print(f"  {offence}")
    else:
        print(
            "OK (A/B/C): narrative.py n importe aucun acces donnees, ne contient "
            "aucun SQL, et ne reference aucune relation raw_/stg_ (la citation "
            "(connector:fact_daily_kpi, pull_id) reste permise -- c est la preuve)."
        )

    if prose:
        print("REGLE D OFFENCES (une phrase de recit ecrite en dur):")
        for offence in prose:
            print(f"  {offence}")
    else:
        print(
            "OK (D): aucune phrase de recit en dur dans les sites declares -- "
            "chaque phrase est composee par cle depuis le catalogue."
        )

    if found or prose:
        return 1 if args.gate else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
