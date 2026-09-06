"""Un écran qui appelle une route SCOPÉE PROJET envoie le Projet — story 67.8.

POURQUOI CE FICHIER EXISTE, ET CE QU'IL AURAIT ATTRAPÉ. `BusinessDomainPicker`
appelait `GET /api/context/business-taxonomy` sans `project_id`. La route refuse
par un 422 `project_id is required` avant d'ouvrir quoi que ce soit, donc le
sélecteur rendait « indisponible » à chaque ouverture des deux dialogues de
Governance, pour toute organisation. Le composant était *honnête* — il distingue
« illisible » de « vide », ce qui est la bonne règle — et c'est précisément ce
qui a laissé la porte fermée si longtemps : un état d'erreur bien écrit se lit
comme une information sur la donnée, pas comme un appel malformé.

UNE GARDE D'EXISTENCE DE ROUTE NE L'AURAIT PAS VU. La route EST montée ;
`screens/routes.json` la contient. L'écart n'était pas « cette adresse n'existe
pas » mais « cette adresse exige un argument que personne n'envoie », et ces
deux-là ne se mesurent pas avec le même instrument.

CE QUE CE FICHIER DÉRIVE, DES DEUX CÔTÉS, À CHAQUE EXÉCUTION :

* côté SERVEUR — les chemins dont le handler refuse sans `project_id` alors que
  le chemin lui-même n'en porte pas (`/api/projects/{id}/...` porte le sien) ;
* côté CONSOLE — toute URL littérale passée à `apiGet`/`apiPost`/… dans
  `ui/admin/src`, hors tests.

Puis il croise. Une liste écrite à la main vieillirait exactement comme le
commentaire qui a causé le bug : le test de ce sélecteur AFFIRMAIT que l'appel
devait partir sans Projet, « une portée projet serait une seconde source de
vérité ». Il gravait le défaut comme la cible.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_CORE = _ROOT / "server" / "core"
_CONSOLE = _ROOT / "ui" / "admin" / "src"

#: Le refus que ce fichier reconnaît. C'est la phrase exacte que les 36 modules
#: d'API emploient déjà ; en reconnaître une seconde formulation demanderait de
#: la fabriquer quelque part.
_REFUSAL = "project_id is required"

#: Les aides d'appel de la console. Toutes passent le chemin en premier argument.
_CALLERS = ("apiGet", "apiPost", "apiPut", "apiPatch", "apiDelete")


def _endpoint_refuses_without_project(tree: ast.AST, endpoint: str) -> bool:
    """Le handler nommé, ou une fonction du même module qu'il appelle, refuse-t-il ?

    Un saut, pas une fermeture transitive : le refus vit soit dans le handler
    (`catalog_api`), soit dans l'aide de portée qu'il appelle immédiatement
    (`business_taxonomy_api._run_scoped`). Deux sauts n'ont pas été nécessaires
    et une profondeur non bornée transformerait la garde en devinette.
    """
    functions: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = node

    def _refuses(node: ast.AST) -> bool:
        return any(
            isinstance(sub, ast.Constant)
            and isinstance(sub.value, str)
            and _REFUSAL in sub.value
            for sub in ast.walk(node)
        )

    handler = functions.get(endpoint)
    if handler is None:
        return False
    if _refuses(handler):
        return True
    for sub in ast.walk(handler):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            callee = functions.get(sub.func.id)
            if callee is not None and _refuses(callee):
                return True
    return False


def _project_scoped_paths() -> set[str]:
    """Les chemins qui EXIGENT `project_id` sans le porter dans leur chemin."""
    scoped: set[str] = set()
    for path in sorted(_CORE.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - un module illisible se dit ailleurs
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "Route" or not node.args:
                continue
            route = node.args[0]
            if not (isinstance(route, ast.Constant) and isinstance(route.value, str)):
                continue
            url = route.value
            if "{project_id}" in url:
                # Le chemin porte déjà le Projet : rien à envoyer en plus.
                continue
            endpoint = next(
                (
                    kw.value.id
                    for kw in node.keywords
                    if kw.arg == "endpoint" and isinstance(kw.value, ast.Name)
                ),
                None,
            )
            if endpoint and _endpoint_refuses_without_project(tree, endpoint):
                scoped.add(url)
    return scoped


_CALL_RE = re.compile(
    r"\b(?:" + "|".join(_CALLERS) + r")\s*(?:<[^>(]*>)?\s*\(\s*([`\"'])(.*?)\1",
    re.S,
)

#: Combien de caractères après l'URL comptent comme « le corps de cet appel ».
#: LE PROJET VOYAGE PAR DEUX CHEMINS, et une garde qui n'en lisait qu'un a rendu
#: DEUX faux positifs au premier tir (`FieldDetailDrawer` -> `/api/datamodel/
#: mappings`, `AlertDestinations` -> `/api/alert-destinations`) : les deux
#: envoient `project_id` dans le CORPS, ce que la route lit tout aussi bien
#: (`_project_id(request, body)`). Accuser un appel correct est le défaut que
#: cette famille de gardes existe pour ne pas commettre.
_BODY_WINDOW = 400


def _console_calls() -> list[tuple[Path, str, str]]:
    """Toute URL littérale passée à une aide d'appel, avec la suite de l'appel."""
    calls: list[tuple[Path, str, str]] = []
    for path in sorted(_CONSOLE.rglob("*.ts*")):
        if "__tests__" in path.parts or path.name.endswith(".d.ts"):
            continue
        text = path.read_text(encoding="utf-8")
        for match in _CALL_RE.finditer(text):
            url = match.group(2)
            body = text[match.end() : match.end() + _BODY_WINDOW]
            calls.append((path, url, body))
    return calls


def _path_of(url: str) -> str:
    return url.split("?", 1)[0]


def _offenders(calls, scoped) -> list[str]:
    """La règle, isolée pour qu'elle soit jouable sur un appel synthétique.

    Sans ce découpage, le seul test possible serait « le dépôt est propre », qui
    passe aussi quand la garde n'accuse plus rien.
    """
    out: list[str] = []
    for source, url, body in calls:
        if _path_of(url) not in scoped:
            continue
        # Dans l'URL, ou dans le corps : la route lit les deux
        # (`_project_id(request, body)`), donc la garde aussi.
        if "project_id=" in url or "project_id" in body:
            continue
        out.append(f"{source} -> {url}")
    return sorted(out)


def test_the_console_sends_the_project_to_every_route_that_demands_it() -> None:
    scoped = _project_scoped_paths()
    assert scoped, (
        "aucune route scopée projet n'a été dérivée -- la garde ne mesure plus "
        "rien. Le refus reconnu est la phrase exacte "
        f"{_REFUSAL!r} ; si elle a été reformulée, c'est ICI qu'il faut le dire."
    )

    offenders = _offenders(
        [
            (source.relative_to(_ROOT).as_posix(), url, body)
            for source, url, body in _console_calls()
        ],
        scoped,
    )

    assert not offenders, (
        "un écran appelle une route qui refuse sans `project_id` et ne l'envoie "
        "pas. Le serveur répondra 422 AVANT d'ouvrir quoi que ce soit, et l'écran "
        "affichera son état d'erreur -- lequel se lit comme une information sur "
        "la donnée, jamais comme un appel malformé. C'est ainsi que "
        "`BusinessDomainPicker` a rendu « indisponible » pour tout le monde, "
        "story 67.8.\n  " + "\n  ".join(sorted(offenders))
    )


def test_the_derivation_finds_the_route_that_motivated_this_guard() -> None:
    """La garde doit trouver le cas connu, sinon elle ne dérive rien d'utile.

    Sans cette assertion, une expression régulière cassée rendrait un ensemble
    vide et le test ci-dessus passerait en ne mesurant rien -- exactement la
    jauge collée au vert que ce dépôt refuse ailleurs.
    """
    assert "/api/context/business-taxonomy" in _project_scoped_paths()


def test_the_console_scan_actually_reads_calls() -> None:
    """Et l'autre moitié doit lire, elle aussi."""
    calls = _console_calls()
    assert len(calls) > 50, f"seulement {len(calls)} appels lus dans la console"
    assert any("/api/context/business-taxonomy" in url for _p, url, _b in calls)


def test_the_guard_still_catches_a_call_that_sends_the_project_nowhere() -> None:
    """Et elle doit pouvoir ACCUSER : sinon elle ne mesure que son indulgence.

    Accepter le corps a fait disparaître deux faux positifs réels
    (`FieldDetailDrawer`, `AlertDestinations`) ; restait à vérifier que la même
    souplesse ne fait pas disparaître les VRAIS. Les trois appels ci-dessous ont
    exactement les trois formes qui comptent, dont celle que
    `BusinessDomainPicker` avait avant sa réparation.
    """
    scoped = _project_scoped_paths()
    url = "/api/context/business-taxonomy"
    assert url in scoped

    accused = _offenders(
        [
            ("Picker.tsx", url, ");" + chr(10) + "      if (cancelled) return;"),
            ("Ok1.tsx", url + "?project_id=proj_EXAMPLE", ");"),
            ("Ok2.tsx", url, ", { project_id: projectId });"),
        ],
        scoped,
    )
    assert accused == [f"Picker.tsx -> {url}"]
