#!/usr/bin/env python3
r"""Ce qu'on ATTEND de chaque ecran, et si on l'a -- derive, jamais ecrit a la main.

POURQUOI CE SCRIPT EXISTE. Jean, 2026-08-03, apres avoir vu un referentiel
ecran-par-ecran produit en deux minutes par lecture de documentation : « c'est
pas parfait [...] il l'a fait par lecture de documentation possiblement pas
toujours a jour, mais au moins il a recoupe les informations, ce qu'on attend de
chaque UI. A toi de trouver un moyen que ca soit reel et tracable. »

Le referentiel avait la bonne FORME et un contenu invérifiable : il annoncait
26 fichiers (en listait 31, le repertoire en contient 8), citait cinq routes qui
n'existent pas (`/context/graph`, `/data/datastreams/new`...), et donnait trois
verdicts « conforme » que la mesure contredit -- dont « validation
Currency/Timezone dans CreateProject.tsx » alors que ce fichier ecrit
`currency: "EUR"` en dur, ligne 45.

CE QUI MANQUAIT N'ETAIT PAS L'INVENTAIRE. `scripts/screens.py` resout deja les
36 ecrans vers leur spec, leur composant monte, leurs tests et leur commande.
Ce qui manquait est la MISSION de l'ecran et les FONCTIONS qu'on en attend --
et surtout un moyen que leur etat ne soit pas une opinion.

    python scripts/screen_expectations.py           # regenere screens/expectations.md
    python scripts/screen_expectations.py --gate    # non-zero si la page est perimee
                                                    # ou si une declaration est cassee

LE PARTAGE DES ROLES, qui est tout l'interet :

  - `screens/expectations.json` DECLARE : la mission de l'ecran, et une ligne par
    fonction attendue, chacune avec la preuve qui la rendrait vraie ;
  - ce script RESOUT ces preuves contre le depot et calcule l'etat.

Une fonction attendue mais absente n'est PAS une erreur de la porte : c'est le
travail restant, et c'est precisement ce qu'on veut voir. La porte rougit quand
la DECLARATION est cassee -- un ecran qui n'existe plus au board, un type de
preuve inconnu -- ou quand la page generee ne correspond plus au depot.

LES QUATRE TYPES DE PREUVE, volontairement peu nombreux :

    file    un chemin du depot doit exister
    mounted le composant de l'ecran doit etre monte par une route (lu du board)
    route   "POST /api/..." doit etre servi par server/core
    calls   le FRONT appelle quelque chose, la ou `route` ne prouve que le
            serveur. Un NOM DE COMPOSANT (`FooDialog`) exige un import reel ;
            tout le reste (`rule-sets/commands`, un chemin d'API) reste une
            recherche de chaine dans ui/admin/src

`route` + `calls` ensemble sont ce qui attrape la classe la plus frequente du
depot : le serveur calcule, l'ecran jette.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: LE BOARD EST LU PAR SON PROPRIETAIRE, PAS PAR SON CHEMIN.
#: `screens/board.json` est un CACHE de `screens.py resolve()`. Ce fichier le
#: lisait en direct, comme `working_context_corpus.py` : trois lecteurs, dont un
#: seul sait quand il est perime. `screens.load_board()` porte cette garde
#: depuis le 2026-08-21 (`board_is_stale`) et rend la meme liste, resolue depuis
#: les sources quand le cache a vieilli.
from screens import load_board  # noqa: E402

DECLARATIONS = ROOT / "screens" / "expectations.json"
JOURNEYS = ROOT / "screens" / "journeys.json"
QA_JOURNEY = ROOT / "_bmad-output" / "implementation-artifacts" / "qa-journey-status.yaml"
#: `partial` : aucun test n'echoue, mais certains sont BLOQUES (une ecriture
#: en production, un consentement humain). Ce n'est ni un vert ni un rouge, et
#: confondre les deux ferait passer une porte a moitie jouee pour une preuve.
STATUSES = ("pass", "fail", "partial", "not_tested", "blocked", "skipped")
OUT = ROOT / "screens" / "expectations.md"
HISTORY = ROOT / "screens" / "expectations-history.jsonl"
SERVER = ROOT / "server" / "core"
UI = ROOT / "ui" / "admin" / "src"

PROOFS = ("file", "mounted", "route", "calls", "in_file")


def _server_serves(route: str) -> bool:
    """Une route est servie -- y compris quand son chemin est COMPOSE.

    Premiere version : recherche du chemin complet entre guillemets. Elle rendait
    « aucune route » pour `analyze/renders`, servie par
    `Route(f"{_BASE}/renders", ...)` avec `_BASE = "/api/projects/{project_id}/analyze"`.
    Un detecteur qui ne voit pas les f-strings rend le resultat qu'on redoute --
    meme classe que le `git grep -o` qui ne comptait qu'une occurrence par ligne.

    On resout donc `_BASE` module par module avant de comparer, plutot que de
    relacher la recherche sur le dernier segment : `/renders"` matcherait
    n'importe quel autre espace.
    """
    path = route.split(None, 1)[-1].strip()
    base_pattern = re.compile(r"^_BASE\s*=\s*['\"](.+?)['\"]", re.M)
    for source in SERVER.glob("*.py"):
        text = source.read_text(encoding="utf-8", errors="replace")
        if f'"{path}"' in text:
            return True
        found = base_pattern.search(text)
        if found and path.startswith(found.group(1)):
            remainder = path[len(found.group(1)):]
            if f'f"{{_BASE}}{remainder}"' in text:
                return True
    return False


def _ui_mentions(needle: str) -> bool:
    return any(needle in p.read_text(encoding="utf-8", errors="replace")
               for p in UI.rglob("*.ts*") if not _is_test(p))


def _is_test(path: Path) -> bool:
    return "__tests__" in path.parts or path.name.endswith((".test.ts", ".test.tsx"))


#: `FooDialog` -- un identifiant de composant. Une valeur qui n'a pas cette
#: forme (`rule-sets/commands`, `/api/projects/...`) est un fragment d'adresse,
#: et c'est la recherche de chaine qui la resout.
_COMPONENT_NAME = re.compile(r"[A-Z][A-Za-z0-9_]*\Z")

#: Ce qui separe `import … from "…"` : la clause porte les noms, quelle que
#: soit sa forme (`Foo`, `{ A, Foo as Bar }`, `* as Foo`, `Foo, { Bar }`).
#: UN IMPORT TIENT SUR PLUSIEURS LIGNES, et le motif l ignorait (2026-08-17).
#: La classe excluait le retour a la ligne, donc la forme la PLUS COURANTE --
#: une accolade ouverte, un nom par ligne, l accolade fermee -- n etait jamais
#: vue. Mesure sur les 297 fichiers de production : 940 clauses vues contre
#: 1 138 reelles, soit UN IMPORT SUR SIX invisible.
#:
#: Le cout est exactement celui que le docstring de `_ui_imports` decrit plus
#: bas : << un instrument qui ment sur un ecran ment sur tous >>.
#: `CreateProject.tsx` importe `ReportingCurrencyField` sur quatre lignes, et
#: la page annoncait MANQUE -- << la creation de projet code la devise en dur >>
#: -- pour un ecran qui avait justement arrete de le faire.
#:
#: Le point-virgule reste la borne, donc une clause ne peut pas deborder sur
#: l instruction suivante ; seul le retour a la ligne est desormais admis.
_IMPORT_CLAUSE = re.compile(r"""\bimport\s+([^;]*?)\s+from\s*["'][^"']+["']""", re.S)

#: `lazy(() => import("../governance/FooDialog"))` -- l'import dynamique ne
#: nomme rien, il ne porte qu'un chemin ; son dernier segment vaut le nom.
_IMPORT_PATH = re.compile(r"""\bimport\s*\(\s*["']([^"']+)["']""")

#: `import type { Foo }` et `{ type Foo }` amenent un TYPE, jamais un rendu.
_TYPE_ONLY = re.compile(r"\btype\s+[A-Za-z_$][\w$]*")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")


def _ui_imports(name: str) -> bool:
    """Un fichier de production IMPORTE ce composant -- pas : le NOMME.

    La premiere version cherchait la chaine, comme pour un fragment d'adresse.
    Elle rendait donc VRAI sur la declaration du composant lui-meme : le seul
    fichier de `ui/admin/src` a contenir `MdmConflictResolutionDialog` etait
    `MdmConflictResolutionDialog.tsx`, et la page annoncait « OK -- l'ecran
    monte le dialogue de resolution » pour un composant que rien ne monte.

    C'est la MEME faute que le detecteur d'orphelins de
    `scripts/finished_work_audit.py` a corrigee le 2026-08-02 (11 -> 22
    orphelins) : trois choses sauvent un nom que rien n'importe -- une mention
    en commentaire, un homonyme local sans rapport, et un voisin de la meme
    branche morte. Un instrument qui ment sur un ecran ment sur tous.

    Un barrel (`export { Foo } from "./Foo"`) ne compte pas : il ne commence
    pas par `import`. Celui qui importe le nom DEPUIS le barrel, si.
    """
    if not UI.exists():
        return False
    for path in UI.rglob("*.ts*"):
        if _is_test(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if name not in text:          # le cas le plus frequent, et le moins cher
            continue
        for clause in _IMPORT_CLAUSE.findall(text):
            if clause.lstrip().startswith("type "):
                continue
            names = set(_IDENT.findall(_TYPE_ONLY.sub("", clause))) - {"as", "type"}
            if name in names:
                return True
        for spec in _IMPORT_PATH.findall(text):
            if spec.rstrip("/").rsplit("/", 1)[-1].split(".")[0] == name:
                return True
    return False




def qa_gates() -> tuple[dict, list[str], str]:
    """Les portes du parcours EXECUTE, et ce qui cloche dans leur declaration.

    `bati` se derive du depot ; `parcouru` ne se derive pas -- il faut lancer le
    parcours sur le deploye. Ce tracker est donc tenu a la main, et c'est
    precisement pourquoi il a besoin d'une garde : un `pass` sans rapport, ou un
    rapport dont le fichier n'existe pas, est un vert qui ne prouve rien.
    """
    import yaml

    problems: list[str] = []
    if not QA_JOURNEY.exists():
        return {}, ["le tracker de parcours execute a disparu"], "?"
    payload = yaml.safe_load(QA_JOURNEY.read_text(encoding="utf-8"))
    gates = payload.get("gates") or {}
    for name, gate in gates.items():
        # The historical tracker also carries analytical-figure notes under the
        # same YAML section. They are research records, not executable gates.
        if not isinstance(gate, dict) or "status" not in gate:
            continue
        status = str(gate.get("status"))
        if status not in STATUSES:
            problems.append(f"porte {name} : statut inconnu `{status}`")
        if status in ("pass", "fail"):
            if not gate.get("last_run"):
                problems.append(f"porte {name} est `{status}` sans date d'execution")
            report = gate.get("report")
            if not report:
                problems.append(f"porte {name} est `{status}` sans rapport")
            elif not (ROOT / str(report)).exists():
                problems.append(f"porte {name} cite un rapport absent : `{report}`")
    return gates, problems, str(payload.get("last_updated"))[:10]


def _anchor(title: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in title.lower()).strip("-")


def verdict(screen: dict, met: int, of: int) -> str:
    """A FAIRE / INCOMPLET / COMPLET -- calcule, jamais ecrit a la main."""
    if not screen.get("mounted_at") or (of and met == 0):
        return "A FAIRE"
    return "COMPLET" if met == of else "INCOMPLET"


def resolve(expectation: dict, screen: dict) -> tuple[bool, str]:
    """Vrai si la fonction attendue est livree, avec la raison si elle ne l'est pas."""
    kind, value = expectation["proof"], expectation["value"]
    if kind == "file":
        return (ROOT / value).exists(), f"`{value}` absent du depot"
    if kind == "mounted":
        return bool(screen.get("mounted_at")), "l'ecran n'est monte par aucune route"
    if kind == "route":
        return _server_serves(value), f"aucune route `{value}` servie par server/core"
    if kind == "calls":
        if _COMPONENT_NAME.fullmatch(value):
            return _ui_imports(value), f"aucun fichier de ui/admin/src n'importe `{value}`"
        return _ui_mentions(value), f"aucun appel a `{value}` dans ui/admin/src"
    if kind == "in_file":
        # `chemin::chaine` -- la seule preuve qui vise un fichier PRECIS. Elle
        # existe parce qu'une preuve trop large ment : demander « gate_decision
        # apparait dans l'UI » repondait OUI a la question « la publication
        # lit-elle la decision ? », qui est une tout autre question.
        target, needle = value.split("::", 1)
        source = ROOT / target
        if not source.exists():
            return False, f"`{target}` n'existe pas"
        return (needle in source.read_text(encoding="utf-8", errors="replace"),
                f"`{target}` ne mentionne jamais `{needle}`")
    raise ValueError(f"type de preuve inconnu : {kind}")


def board_by_id() -> dict[str, dict]:
    """Le board, par identifiant d'ecran -- et jamais un cache perime.

    Le seam est une FONCTION et non une constante de chemin : c'est le
    proprietaire du cache qui sait s'il a vieilli, et un test qui veut un
    board synthetique remplace cette fonction plutot qu'un fichier.
    """
    return {screen.id: asdict(screen) for screen in load_board()}


def build() -> tuple[str, list[str], dict]:
    board = board_by_id()
    declared = json.loads(DECLARATIONS.read_text(encoding="utf-8"))
    journeys = json.loads(JOURNEYS.read_text(encoding="utf-8"))
    broken: list[str] = []

    out: list[str] = []
    w = out.append
    w("<!-- GENERE par scripts/screen_expectations.py -- ne pas editer a la main. -->")
    w("<!-- Regenerer : python scripts/screen_expectations.py  |  verifier : --gate -->")
    w("")
    w("# Ce qu'on attend de chaque ecran, et ce qu'on a")
    w("")
    w("La colonne **Etat** n'est jamais ecrite a la main : elle est calculee en")
    w("resolvant, contre le depot, la preuve declaree dans `screens/expectations.json`.")
    w("Une fonction `MANQUE` n'est pas une erreur de cette page -- c'est le travail")
    w("restant, rendu visible.")
    w("")
    w("Un `OK` dit : l'artefact existe, il est monte, la route est servie, le front")
    w("l'appelle. Il ne dit pas que quelqu'un a parcouru l'ecran.")
    w("")

    total = delivered = 0
    rows: list[dict] = []
    for screen_id, entry in sorted(declared.items()):
        if screen_id.startswith("_"):   # cles de documentation du fichier
            continue
        screen = board.get(screen_id)
        if screen is None:
            broken.append(f"`{screen_id}` n'existe plus dans screens/board.json")
            continue
        results = []
        for expectation in entry["expects"]:
            if expectation["proof"] not in PROOFS:
                broken.append(f"{screen_id} : type de preuve inconnu "
                              f"`{expectation['proof']}`")
                continue
            ok, why = resolve(expectation, screen)
            results.append((expectation, ok, why))
        met = sum(1 for _, ok, _ in results if ok)
        total += len(results)
        delivered += met
        rows.append({"id": screen_id, "screen": screen, "entry": entry,
                     "results": results, "met": met, "of": len(results),
                     "state": verdict(screen, met, len(results))})

    # --- Vue d'ensemble : ce qui n'est pas fait, mal fait, ou a completer.
    w("## Vue d'ensemble")
    w("")
    w("`A FAIRE` : l'ecran n'est monte nulle part, ou aucune fonction attendue n'est")
    w("livree. `INCOMPLET` : il en manque. `COMPLET` : toutes les fonctions declarees")
    w("sont livrees -- ce qui ne dit pas que l'ecran est bon, seulement qu'il tient ce")
    w("qu'on a ecrit qu'on attendait de lui.")
    w("")
    w("| Ecran | Etat | Livrees | Monte |")
    w("| --- | --- | --- | --- |")
    for row in sorted(rows, key=lambda r: (r["state"] != "A FAIRE",
                                           r["state"] != "INCOMPLET", r["id"])):
        w(f"| [{row['screen']['title']}](#{_anchor(row['screen']['title'])}) "
          f"| **{row['state']}** | {row['met']}/{row['of']} "
          f"| {'oui' if row['screen'].get('mounted_at') else '**non**'} |")
    for screen_id in sorted(set(board) - {k for k in declared if not k.startswith("_")}):
        w(f"| {board[screen_id]['title']} | NON DECLARE | — "
          f"| {'oui' if board[screen_id].get('mounted_at') else '**non**'} |")
    w("")
    w("---")
    w("")

    # --- Chemins transverses : une chaine ne vaut que son maillon le plus faible.
    by_screen = {r["id"]: r for r in rows}
    stops: dict[str, int] = {}
    w("## Chemins utilisateurs transverses")
    w("")
    w("Un ecran `COMPLET` ne prouve rien sur le parcours qui le traverse. C'est la")
    w("faute que CLAUDE.md section 4 nomme : « 11/11 Datastreams crees » annonce")
    w("pendant que tous les ecrans d'apres repondaient 404. Un parcours s'arrete a sa")
    w("PREMIERE etape non livree, et c'est ce point d'arret qui est publie.")
    w("")
    w("Complementaire de `qa-journey-status.yaml` (portes G0..G12), qui mesure le")
    w("DEPLOYE par execution reelle. Ici on derive ce qui est derivable : la chaine")
    w("est-elle batie de bout en bout. Vert ne veut pas dire parcourue.")
    w("")
    gates, gate_problems, gates_dated = qa_gates()
    broken.extend(gate_problems)
    w(f"Parcours execute, dernier releve : **{gates_dated}** — "
      f"{sum(1 for g in gates.values() if str(g.get(chr(115)+'tatus')) == 'pass')} "
      f"portes `pass` sur {len(gates)}.")
    w("")
    for journey_id, journey in journeys.items():
        if journey_id.startswith("_"):
            continue
        w(f"### {journey['name']}")
        w("")
        w(f"_{journey['why']}_")
        w("")
        w("| # | Etape | Ecran | Etat |")
        w("| --- | --- | --- | --- |")
        stops_at = None
        for index, step in enumerate(journey["steps"], start=1):
            row = by_screen.get(step["screen"])
            if row is None:
                state = "**non declare** — aucune attente sur cet ecran"
                ok = False
            elif "expectation" in step:
                match = [(e, k, why) for e, k, why in row["results"]
                         if e["what"] == step["expectation"]]
                if not match:
                    broken.append(f"parcours `{journey_id}` etape {index} : "
                                  f"aucune attente « {step['expectation']} » sur "
                                  f"`{step['screen']}`")
                    continue
                _, ok, why = match[0]
                state = "OK" if ok else f"**MANQUE** — {why}"
            else:
                ok = row["state"] != "A FAIRE"
                state = row["state"]
            # A step can be DECLARED and deliberately not built, without
            # blocking the walk. `LiveGrainImpact` is the first: the wizard
            # publishes without it, so calling steps 5 and 6 "hors d'atteinte"
            # would be as false as calling step 4 delivered. A step is blocking
            # by DEFAULT -- `blocking: false` has to be written down, with the
            # reason, and the reason is rendered here rather than filed away.
            blocking = step.get("blocking", True)
            if not ok and not blocking:
                state = f"**DIFFERE** — {step.get('deferred_because', why)}"
            if not ok and blocking and stops_at is None:
                stops_at = index
            w(f"| {index} | {step['step']} | `{step['screen']}` | {state} |")
        w("")
        stops[journey_id] = stops_at or 0
        claimed = journey.get("proves_gates", [])
        for gate_name in claimed:
            if gate_name not in gates:
                broken.append(f"parcours `{journey_id}` cite la porte `{gate_name}`, "
                              f"absente du tracker execute")
        lived = ", ".join(
            f"`{g}` **{gates[g].get('status')}**"
            + (f" ({str(gates[g].get('last_run'))[:10]})" if gates[g].get("last_run") else "")
            for g in claimed if g in gates) or "aucune porte declaree"
        w(f"Parcouru pour de vrai ? {lived}")
        w("")
        if stops_at is None:
            w("**Chaine batie de bout en bout.** Ce qui ne dit pas qu'elle a ete vecue "
              "-- lire la ligne ci-dessus.")
        else:
            w(f"⛔ **Le parcours s'arrete a l'etape {stops_at}** — "
              f"{len(journey['steps']) - stops_at} etape(s) apres elle sont hors "
              f"d'atteinte, quel que soit leur propre etat.")
        w("")
    w("---")
    w("")

    # --- Detail par ecran.
    for row in rows:
        screen, entry = row["screen"], row["entry"]
        w(f"## {screen['title']}")
        w("")
        w(f"`{row['id']}` — {entry['mission']}")
        w("")
        w(f"**{row['state']}** · {row['met']}/{row['of']} livrees · route "
          f"`{screen['route']}` · "
          + (f"monte en `{screen['mounted_at']}`" if screen.get("mounted_at")
             else "**monte par aucune route**"))
        w("")
        screenshots = entry.get("screenshots") or []
        if screenshots:
            w("Preuves visuelles locales :")
            w("")
            for screenshot in screenshots:
                w(f"![{screenshot['label']}]({screenshot['path']})")
                w("")
        w("| Ce qu'on en attend | Etat | Preuve |")
        w("| --- | --- | --- |")
        for expectation, ok, why in row["results"]:
            state = "OK" if ok else f"**MANQUE** — {why}"
            w(f"| {expectation['what']} | {state} | `{expectation['proof']}: "
              f"{expectation['value']}` |")
        w("")

    missing_screens = sorted(set(board) - {k for k in declared if not k.startswith("_")})
    w("---")
    w("")
    declared_count = sum(1 for key in declared if not key.startswith(chr(95)))
    w(f"**{delivered} sur {total}** fonctions attendues sont livrees, "
      f"sur **{declared_count} ecrans declares** parmi les {len(board)} du board.")
    w("")
    w("## Ecrans sans attente declaree")
    w("")
    w("Personne ne peut dire ce qu'on en attend sans relire le code. C'est le")
    w("travail restant, et il se voit :")
    w("")
    for screen_id in missing_screens:
        w(f"- `{screen_id}` — {board[screen_id]['title']}")
    w("")
    tally = {r["id"]: [r["met"], r["of"], r["state"]] for r in rows}
    tally["_journeys"] = stops
    return "\n".join(out).rstrip() + "\n", broken, tally


def record(tally: dict) -> None:
    """Une ligne d'historique par CHANGEMENT, jamais une par execution.

    Le progres est la difference entre deux lignes -- pas une affirmation. Une
    execution qui ne change rien n'ecrit rien, sinon le journal se remplit de
    bruit et la difference devient illisible.
    """
    journeys = tally.pop("_journeys", {})
    delivered = sum(v[0] for v in tally.values())
    of = sum(v[1] for v in tally.values())
    row = {"date": datetime.now(UTC).strftime("%Y-%m-%d"),
           "delivered": delivered, "of": of, "screens": len(tally),
           # 0 = chaine batie de bout en bout ; sinon le numero de l'etape qui
           # l'arrete. C'est ce nombre qu'on veut voir bouger, pas un total.
           "journeys_stop_at": dict(sorted(journeys.items())),
           "by_screen": {k: v[2] for k, v in sorted(tally.items())}}
    previous = None
    if HISTORY.exists():
        lines = [line for line in HISTORY.read_text(encoding="utf-8").splitlines() if line.strip()]
        if lines:
            previous = json.loads(lines[-1])
    previous_without_date = {
        key: value for key, value in (previous or {}).items() if key != "date"
    }
    row_without_date = {key: value for key, value in row.items() if key != "date"}
    if previous and previous_without_date == row_without_date:
        return
    with HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + chr(10))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gate", action="store_true")
    args = ap.parse_args()

    page, broken, tally = build()
    if args.gate:
        if broken:
            for problem in broken:
                print(problem, file=sys.stderr)
            print("\nUne DECLARATION est cassee. Une fonction manquante ne fait pas "
                  "rougir cette porte -- une declaration qui ment, oui.", file=sys.stderr)
            return 1
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != page:
            print("screens/expectations.md est perime -- "
                  "`python scripts/screen_expectations.py` le regenere.", file=sys.stderr)
            return 1
        print("screens/expectations.md est a jour")
        return 0

    OUT.write_text(page, encoding="utf-8")
    record(tally)
    print(f"{OUT.relative_to(ROOT)} regenere")
    for problem in broken:
        print("  ", problem)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
