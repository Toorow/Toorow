#!/usr/bin/env python3
r"""Le CORPUS metier projete dans le Context Hub -- derive du depot, jamais redige.

POURQUOI CE FICHIER EXISTE.

Jean, 2026-08-03 : « ton instance projet toorow va avoir l'ensemble des metiers,
comme data analyst : qu'est-ce qu'il doit faire, comment le faire, qu'est-ce
qu'il peut utiliser, verifier. Le nom des dimensions des reports, le detail des
sources. [...] c'est pas de mettre des choses d'audit, c'est d'avoir une
documentation de metier claire et comment proceder », puis « techniquement tu
peux rentrer le metier que tu veux, il doit savoir se debrouiller », puis --
apres une premiere tentative -- « te l'avais dit que tu avais injecte n'importe
quoi : faut que la donnee soit vraiment dans l'idee de ce qui est ecrit dans la
documentation ».

LES DEUX FAUTES, DANS L'ORDRE OU ELLES ONT ETE COMMISES.

1. **Une projection creuse.** Trente-six cartes disant « ecran monte : oui »
   forment un tableau de bord, pas le savoir-faire d'un metier. Il manquait la
   matiere : dimensions, sources, structure de base, architecture, attentes.
2. **Des Skills INVENTEES.** La correction de la premiere faute a produit des
   procedures que j'avais REDIGEES -- des etapes, des pieges, des verifications
   de ma main, presentes comme le metier. C'est exactement ce que CLAUDE.md
   interdit : ne jamais inventer une decision de conception et la presenter
   comme une reparation.

D'OU LA REGLE DE CE FICHIER : **aucune etape de Skill n'est ecrite ici.** Chacune
est LUE dans un document ratifie, et porte sa source. Ce que la documentation ne
dit pas, ce fichier ne le dit pas non plus -- il le declare manquant.

CE QUE LA DOCUMENTATION PORTE REELLEMENT, mesure le 2026-08-03 :

  * **une seule** table « Setup steps — what delivers each one », dans
    `datastream-workbench-and-wizard.md` : 9 etapes. C'est la seule procedure
    ratifiee du depot.
  * **22 documents** portent un « Incomplete if » -- le critere d'acceptation
    d'une surface. C'est ce qui repond a « qu'est-ce qui est attendu ».
  * **18 surfaces n'ont AUCUNE table d'etapes**, et `SURFACE-STATE.md` le dit en
    toutes lettres : « personne ne peut repondre "ai-je tout ce qu'il faut ?"
    sans relire le code. C'est le travail restant. » Ce manque est projete comme
    un manque, pas comble par de la prose.

    python scripts/working_context_corpus.py     # ce que le corpus contient
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: THE FOURTH READER OF THE `Incomplete if` LIST, AND IT PUBLISHED.
#:
#: This file used to own its own walk: first `## Incomplete if` block, stop at
#: the next `##`, split on dashes or numbers. Measured 2026-08-21 against the
#: canonical parser over the 29 ratified pages: **359 criteria against 546**, 22
#: pages in disagreement, **ZERO** for `page-structure`, `user-bridge` and
#: `visualization-and-rendering`, and a different answer at INDEX 0 for
#: `data-path`, `datastream-workbench-and-wizard` and those same three. The
#: command is in `server/tests/core/test_criteria_parser.py`.
#:
#: That is worse here than in an audit script: `skills()` and `knowledge()` feed
#: `scripts/push_working_context.py`, which pushes them into the Context Hub. A
#: private miscount is a wrong number; this one publishes a wrong list to every
#: agent that reads the Hub.
#:
#: `check_ledger_anchors.py` and `screens.py` were already made to consume the
#: one function on 2026-08-17 and 2026-08-21. This is the same repair, and the
#: test that found it does not name the modules it checks: it looks for any
#: module that matches on the marker in code and does not import this symbol.
from finished_work_audit import incomplete_if  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "product-architecture"
MODULES = ROOT / "server" / "modules"

PROJECTED = (
    "Projected from the repository — do not edit here; the next push would overwrite it."
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Les metiers sont DECLARES, pas codes.
#
# Jean, 2026-08-03 : « ca doit etre adaptable pour comprendre le contexte dev,
# tester, DevOps, manager, help center par exemple ». Ajouter un metier ne doit
# donc pas demander de toucher a ce fichier : une entree dans
# `working_context_trades.json` suffit -- sa racine semee, ses documents, ses
# operations, ses familles de matiere.
#
# ⚠️ TOUT CE QUI EST PROJETE EST EN ANGLAIS. « attention tout doit etre en
# anglais » : c'est la regle du contenu produit, et une projection dans le
# Context Hub EST du contenu produit. Les commentaires et docstrings de ce
# fichier restent en francais -- ils sont du code, pas du contenu.
# ---------------------------------------------------------------------------

DECLARATION = json.loads(_read(Path(__file__).resolve().parent / "working_context_trades.json"))

#: Le metier auquel revient un document non declare. Defaut VOLONTAIRE : un
#: document sans proprietaire se voit, au lieu de disparaitre.
FALLBACK_TRADE = "manager"

TRADES: dict[str, dict] = {
    slug: {
        "root": trade["root"],
        "name": trade["name"],
        "description": f"{trade['description']} {PROJECTED}",
    }
    for slug, trade in DECLARATION["trades"].items()
}

#: Quel metier possede quel document -- derive de la declaration, jamais retape.
TRADE_OF_DOCUMENT = {
    stem: slug
    for slug, trade in DECLARATION["trades"].items()
    for stem in list(trade.get("documents", [])) + list(trade.get("capabilities", []))
}


def _trades_with(family: str) -> list[str]:
    return [slug for slug, trade in DECLARATION["trades"].items()
            if family in trade.get("families", [])]


def _trade_of_reference(name: str) -> str | None:
    for slug, trade in DECLARATION["trades"].items():
        if name in trade.get("references", []):
            return slug
    return None


# ---------------------------------------------------------------------------
# Les Skills -- LUES, pas ecrites
# ---------------------------------------------------------------------------


def _setup_steps() -> list[tuple[str, str, str]]:
    """Les etapes ratifiees, lues dans la table generee de `SURFACE-STATE.md`.

    La table est elle-meme derivee des documents par `scripts/surface_state.py`,
    qui resout chaque citation contre le depot. On lit donc une derivation, pas
    une prose -- et elle rougit quand le depot bouge.
    """
    page = _read(DOCS / "SURFACE-STATE.md")
    steps = []
    for line in page.splitlines():
        match = re.match(r"^\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(\S+)\s*\|\s*(.+?)\s*\|$", line)
        if match:
            steps.append((match.group(2), match.group(3), match.group(4)))
    return steps


def _product_sheet_steps() -> list[dict]:
    """Les etapes de redaction d'une fiche produit, LUES dans `product-sheet.md`.

    La table « Writing steps » du document est la source ; la Skill projetee
    rougit si le document change. Format attendu : `| n | action | label | acts on |`.
    """
    # BORNEE A SA SECTION, ET C'EST LE DEFAUT QUE CELA REPARE (2026-09-05).
    # Le motif `| n | mot | ... | ... |` decrit aussi la ligne 1 et la ligne 2 de
    # CHAQUE fiche d'exemple du document (`| 2 | Audience | ... |
    # navigation/governance.ts:26 |`), et la Skill projetee les portait comme des
    # pas d'ecriture : un modele qui la prend lisait << etape 2 : Audience >>
    # pointant un fichier de navigation. `check_skill_targets.py` le disait par
    # la seule de ces cibles qui ressemble a un chemin. La table des pas vit sous
    # `## Writing steps` et s'arrete au titre suivant ; hors de ces bornes, une
    # ligne de tableau n'est pas un pas.
    page = _read(DOCS / "product-sheet.md")
    lines = page.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith("## Writing steps"))
        end = next(
            (i for i, line in enumerate(lines) if i > start and line.startswith("## ")),
            len(lines),
        )
    except StopIteration:  # le document a perdu sa section : aucun pas, pas d'invention
        return []
    steps = []
    for line in lines[start:end]:
        match = re.match(r"^\|\s*(\d+)\s*\|\s*(\w[\w-]*)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|$", line)
        if not match:
            continue
        step: dict = {
            "step": int(match.group(1)),
            "action": match.group(2),
            "label": match.group(3),
            # Le validateur serveur exige une cible par pas (target/command/tool)
            # -- meme quand le pas agit sur la fiche elle-meme.
            "target": match.group(4).strip("`"),
        }
        steps.append(step)
    return steps


def _endpoint_facts(module: str, endpoint: str) -> dict | None:
    """Ce qu'une route EXIGE, lu dans le handler -- pas dans une documentation.

    Jean, 2026-08-03 : « tu dois pouvoir repondre comment ajouter un skill,
    comment ajouter une valeur dans mon glossaire, comment on ecrit la
    gouvernance ». Ces reponses existent, et elles sont dans le CODE : la route,
    la methode, les cles que le handler lit dans le corps, et le detail qui a
    coute quinze essais -- ou passe le `project_id`.

    Rien n'est ecrit ici. Une reponse ecrite a la main serait vraie le jour ou on
    l'ecrit ; celle-ci suit la route.
    """
    path = ROOT / "server" / "core" / f"{module}.py"
    if not path.exists():
        return None
    source = _read(path)
    routes = [
        (methods.replace('"', "").replace("'", "").strip(), route)
        for route, name, methods in re.findall(
            r'Route\(\s*"([^"]+)"\s*,\s*endpoint=(\w+)\s*,\s*methods=\[([^\]]+)\]', source)
        if name == endpoint
    ]
    if not routes:
        return None
    body = re.search(rf"(?:async )?def {re.escape(endpoint)}\b.*?(?=\n(?:async )?def )",
                     source, re.S)
    handler = body.group(0) if body else ""
    keys = sorted(set(re.findall(r'(?:body|patch)\.get\(\s*"(\w+)"', handler)))
    scope = None
    if "_required_url_project(request)" in handler or "_project_id(request)" in handler:
        scope = "query string — `?project_id=…`"
    elif "_with_scope(" in handler:
        scope = "query string — `?project_id=…` (through `_with_scope`)"
    elif '"project_id"' in handler:
        scope = "request body"
    return {"routes": routes, "keys": keys, "scope": scope}


def _agents() -> dict[str, dict]:
    """Les roles DECLARES du depot, lus dans `.claude/agents/*.md`.

    Le decoupage dev / relecteur / manager n'est pas a inventer : il est ecrit,
    et il est meme APPLIQUE -- la liste `tools` de chaque agent decide qui peut
    ecrire. Les quatre lentilles de relecture sont en lecture seule ; seul le
    reparateur porte `Edit` et `Write`.
    """
    out: dict[str, dict] = {}
    for path in sorted((ROOT / ".claude" / "agents").glob("*.md")):
        text = _read(path)
        head = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        if not head:
            continue
        fields = dict(
            re.findall(r"^(name|description|tools|model):\s*(.+)$", head.group(1), re.M)
        )
        out[path.stem] = {**fields, "body": head.group(2)}
    return out


def _numbered(body: str, heading: str) -> list[str]:
    """Les pas numerotes d'une section, tels qu'ils sont ecrits."""
    section = re.search(rf"^## {re.escape(heading)}\s*$(.+?)(?=^## |\Z)", body, re.M | re.S)
    if not section:
        return []
    steps = re.findall(r"^\d+\.\s+(.+?)(?=^\d+\.\s|\Z)", section.group(1), re.M | re.S)
    return [" ".join(step.split()) for step in steps]


def _tags(skill: dict) -> list[str]:
    """Les metriques canoniques qu'une Skill concerne -- et rien d'autre.

    ⚠️ J'AI DETOURNE CE CHAMP, et la mesure l'a montre. `mdm_tags` n'est pas un
    champ de mots-cles : la console l'intitule « MDM tags — Canonical metrics »
    et le lie par cases a cocher au catalogue `app.target_fields`
    (`SkillEditorDrawer.tsx`). J'y ecrivais `developer`, `role-sequence`,
    `screen-fixer` -- une folksonomie. Mesure : sur le catalogue local de 13
    champs approuves, **zero** de mes tags resolvait.

    Deux raisons de ne rien y mettre plutot que de continuer :

      * le sens. Un tag qui ne nomme pas un champ gouverne rend le champ inutile
        pour ce a quoi il sert -- savoir quelles metriques une Skill concerne ;
      * le cout nul. Ces tags n'apportaient RIEN de mesurable : `context_search`
        ne lit pas le frontmatter (AI-154), donc ils n'ont jamais servi au
        rappel. Les retirer ne perd aucune capacite.

    Aucune Skill de ce corpus ne concerne une metrique canonique en particulier
    -- elles portent des procedures, pas des mesures. La liste est donc vide, et
    c'est une reponse, pas un oubli.
    """
    return []


def _common_errors_for(commands: list[str]) -> list[dict]:
    """Symptome -> causes, DERIVE de ce que les scripts exigent d'eux-memes.

    Il n'y avait aucune source structuree : sur les 50 `SKILL.md` du depot, UN
    SEUL portait une section de ce genre, et en prose. Mais une source EXISTE et
    elle est mecanique -- ce qu'un script lit dans l'environnement. C'est
    exactement la panne qui a coute une demi-session : `PG_BIN` non pose, le
    script imprimant son aide, et l'aide lue comme un refus.

    On ne derive donc que ce qu'on sait vraiment : une commande qui appelle un
    script du depot echoue quand une de ses variables manque. Inventer d'autres
    causes serait remettre de la prose la ou l'on vient d'en retirer.
    """
    errors: list[dict] = []
    for command in commands:
        match = re.search(r"(scripts/[\w./-]+\.py)", command)
        if not match:
            continue
        path = ROOT / match.group(1)
        if not path.exists():
            continue
        needed = sorted({
            name for name in re.findall(
                r"environ(?:\.get)?\(\s*[\"']([A-Z][A-Z0-9_]+)[\"']", _read(path))
            if name not in {"PATH", "HOME", "PWD", "TMPDIR", "TEMP", "USERPROFILE", "CI"}
        })
        if needed:
            errors.append({
                "symptom": f"`{match.group(1)}` refuses to run or prints its own help",
                "causes": [f"`{name}` is unset in the environment" for name in needed],
            })
    return errors


def _bullets(body: str, heading: str) -> list[str]:
    """Les puces d'une section, telles qu'elles sont ecrites."""
    section = re.search(rf"^## {re.escape(heading)}\s*$(.+?)(?=^## |\Z)", body, re.M | re.S)
    if not section:
        return []
    items = re.findall(r"^- (.+?)(?=^- |\Z)", section.group(1), re.M | re.S)
    return [" ".join(item.split()) for item in items]


def role_skills() -> list[dict]:
    """Le parcours de livraison, derive des roles declares et des portes cablees.

    Jean : « si tu dois faire un cas de bout en bout, c'est le cas du dev, du
    manager, du relecteur, avec l'ordre des verifications et les taches que
    chacun doit faire, avec le bon split. »

    Il n'y a rien a concevoir : `.claude/agents/*.md` declare les roles,
    `page-orchestrator.md` declare l'ORDRE, `screen-fixer.md` declare la
    sequence et ses refus, et `.claude/settings.json` cable les portes de fin de
    tache -- dans un ordre qui est celui du fichier.
    """
    agents = _agents()
    built: list[dict] = []

    # 1. LE RELECTEUR. Un digest, quatre lentilles en parallele, un recoupement.
    orchestrator = agents.get("page-orchestrator")
    if orchestrator:
        steps = re.findall(r"^## (Step \d+ — .+)$", orchestrator["body"], re.M)
        lenses = [
            (name, agents[name]["description"].split(".")[0])
            for name in sorted(agents)
            if name.startswith("screen-review-")
        ]
        built.append({
            "trade": "reviewer",
            "kind": "role-sequence",
            "name": "Review one page — the declared order",
            "description": "One digest, four lenses in parallel, one cross-check.",
            "source": ".claude/agents/page-orchestrator.md",
            "lines": [
                *[f"{index}. **{step}**" for index, step in enumerate(steps, 1)],
                "",
                "**The four lenses, each read-only, each on the same target:**",
                *[f"- `{name}` — {scope}." for name, scope in lenses],
                "",
                "Reading the target once first is not a preference: "
                "« four lenses reading four different targets produce findings "
                "that cannot be compared »."
            ],
            "note": (
                "The orchestrator delegates and never reads the code itself — "
                "« you would produce a fifth opinion instead of reconciling four ». "
                "The split is enforced, not advised: every lens declares "
                "`Read, Grep, Glob` and none declares `Edit`."
            ),
        })

    # 2. LE DEVELOPPEUR. Sa sequence, et surtout ses refus.
    fixer = agents.get("screen-fixer")
    if fixer:
        refusals = _bullets(fixer["body"], "What you never do")
        built.append({
            "trade": "developer",
            "kind": "role-sequence",
            "name": "Fix one screen — the declared sequence",
            "description": "Six steps, and the four things that are never done.",
            "source": ".claude/agents/screen-fixer.md",
            "lines": [
                *[f"{index}. {step}" for index, step in enumerate(
                    _numbered(fixer["body"], "The sequence"), 1)],
                "",
                "**Never:**",
                *[f"- {' '.join(item.split())}" for item in refusals],
            ],
            "note": (
                "Step 6 is the one that separates a fix from a patch: "
                "« One screen repaired while its 35 siblings carry the same defect "
                "is not a fix. » And closing an `Incomplete if` criterion is the "
                "caller's decision, never the fixer's."
            ),
        })

    # 2 bis. LE TESTEUR. Ses portes de parcours, et ce que chacune refuse.
    #
    #   Mesure du 2026-08-03 : avant ceci, le metier `tester` portait UNE Skill --
    #   les criteres d'acceptation d'un document -- sans sequence, sans commande,
    #   et introuvable depuis ses propres questions (« test », « gate », « prove »
    #   rendaient l'architecture ou la Skill du manager). Un metier qui ne repond
    #   pas a sa question d'entree ne sert a personne.
    gates_dir = ROOT / "e2e" / "gates"
    gates = []
    if gates_dir.exists():
        for path in sorted(p for p in gates_dir.glob("*.py") if not p.name.startswith("_")):
            source = _read(path)
            gate = re.search(r'^GATE\s*=\s*["\'](\w+)["\']', source, re.M)
            title = re.search(r'^TITLE\s*=\s*["\'](.+?)["\']', source, re.M)
            if gate:
                gates.append((gate.group(1), title.group(1) if title else path.stem))
    if gates:
        built.append({
            "trade": "tester",
            "kind": "role-sequence",
            "name": "Run the journey gates — prove a path, or say it is not written",
            "description": (
                "Test the end-to-end journey: which gates exist, how to run one, and "
                "what a green result proves. Prove, verify, check a path."
            ),
            "source": "e2e/gates/ + Makefile — read from the gates themselves",
            "steps": [
                {"step": 1, "action": "run", "label": "List the gates that actually exist",
                 "command": "uv run python e2e/runner.py --list",
                 "stop_if": "the gate you want is absent — it is NOT written, not untested"},
                {"step": 2, "action": "run",
                 "label": "Point the harness at a local stack, never production",
                 "command": "python scripts/disposable_postgres.py up",
                 "stop_if": "PG_BIN is unset — the script names it in its own help"},
                {"step": 3, "action": "run", "label": "Run the server suite against that stack",
                 "command": "make test-full",
                 "stop_if": "it refuses without a DSN, and prints the three commands to run"},
                *[
                    {"step": 4 + index, "action": "run", "label": f"{gate} — {title}",
                     "command": f"uv run python e2e/runner.py {gate}"}
                    for index, (gate, title) in enumerate(gates)
                ],
            ],
            "common_errors": _common_errors_for(
                ["python scripts/disposable_postgres.py up",
                 "uv run python e2e/runner.py --list"]),
            "acceptance": [
                "Every number reported carries the command that produces it",
                "A gate reported as failing was run against a local stack, not production",
                "An absent gate is reported as NOT WRITTEN, never as not tested",
                "A green gate is reported as built, routed and covered — never as walked",
            ],
            "lines": [
                f"**{len(gates)} gates are written**, out of G0..G12 named by the journey:",
                "",
                *[f"- `{gate}` — {title}" for gate, title in gates],
                "",
                "The others are not `not_tested`: they are **not written**. Confusing the "
                "two makes you wait for a run that will never happen.",
            ],
            "note": (
                "An `OK` means built, routed and covered — not walked through. And a "
                "harness that writes into production leaves plan versions nobody can "
                "delete: twenty-nine of them prove it."
            ),
        })

    # 3. LE MANAGER. Les portes, dans l'ordre ou elles sont cablees.
    settings = ROOT / ".claude" / "settings.json"
    if settings.exists():
        hooks = json.loads(_read(settings)).get("hooks", {}).get("Stop", [])
        commands = [h["command"] for entry in hooks for h in entry.get("hooks", [])]
        built.append({
            "trade": "manager",
            "kind": "role-sequence",
            "name": "Close a task — the gates that must pass, in order",
            "description": f"The {len(commands)} gates wired to the end of every task.",
            "source": ".claude/settings.json (Stop hook)",
            "lines": [f"{index}. `{command}`" for index, command in enumerate(commands, 1)],
            "note": (
                "These run whether or not anyone remembers them — that is the point. "
                "A gate that refuses while printing what to do is worth more than a "
                "checklist nobody opens. The order is the file's order."
            ),
        })
    return built


_VALIDATOR = ROOT / "server" / "core" / "context_store.py"


def _validator_source() -> str:
    """La partie du store qui VALIDE une Skill -- avant le premier writer."""
    text = _read(_VALIDATOR)
    return text[: text.index("def _clean_owner")]


def skill_authoring() -> tuple[dict, dict]:
    """La Skill et la Knowledge du metier « Skill Editor ».

    Jean, 2026-08-03 : « normalement tu dois ecrire un business qui s'appelle
    skill editor, qui doit connaitre toutes les regles pour ecrire correctement,
    et lui attacher une documentation ».

    Les regles ne sont PAS a rediger : depuis le meme jour, elles sont dans le
    validateur. Chaque `raise ValueError` de
    `context_store.validate_procedure_frontmatter` EST une regle, formulee comme
    un refus -- c'est-a-dire la seule forme qui ne peut pas mentir, puisque c'est
    elle que le produit applique.

    La Skill est ecrite dans la forme qu'elle enseigne : elle porte des `steps`
    avec leur action, leur cible et leur branche d'echec.
    """
    source = _validator_source()
    actions = sorted(re.findall(r'"([a-z-]+)"',
                                re.search(r"STEP_ACTIONS = frozenset\(\s*\{([^}]+)\}",
                                          source, re.S).group(1)))
    limits = dict(re.findall(r"^_MAX_(\w+) = (\d+)", source, re.M))
    refusals = [
        " ".join((a or b).split())
        for a, b in re.findall(r'raise ValueError\(\s*(?:f?"([^"]+)"|f"([^"]+)")', source)
    ]

    skill = {
        "trade": "skill-editor",
        "kind": "authoring-rule",
        "name": "Write a Skill the console can render",
        "description": (
            "The order the validator checks in, and what it refuses. "
            "Writing a Skill in any other order means finding out at the last gate."
        ),
        "source": "server/core/context_store.py — read from the validator itself",
        "steps": [
            {"step": 1, "action": "read", "label": "Read the trade this Skill belongs to",
             "target": "docs/product-architecture/context-hub.md",
             "stop_if": "the trade has no classification under a seeded Business Domain"},
            {"step": 2, "action": "edit", "label": "Name it, uniquely for the project",
             "target": "frontmatter `name`",
             "stop_if": "a Skill already holds this name — the server answers 409"},
            {"step": 3, "action": "edit",
             "label": "Describe it with the words someone will type",
             "target": "frontmatter `description`"},
            {"step": 4, "action": "edit", "label": "Write the sequence",
             "target": "frontmatter `steps`"},
            {"step": 5, "action": "edit",
             "label": "State the acceptance criteria for the whole Skill",
             "target": "frontmatter `acceptance`"},
            {"step": 6, "action": "edit", "label": "Bind each known symptom to its causes",
             "target": "frontmatter `common_errors`"},
            {"step": 7, "action": "run", "label": "Let the validator refuse it",
             "command": "uv run python -m pytest tests/core/test_procedure_frontmatter_steps.py"},
        ],
        "common_errors": [
            {
                "symptom": "the server answers 422 frontmatter_invalide",
                # ⚠️ Les refus qui portent une accolade sont des GABARITS non
                # rendus (`{index}`, `{_MAX_STEPS}`) : lisibles dans le code,
                # incomprehensibles hors de lui. Une cause qu'on ne peut pas lire
                # n'aide personne, donc on ne la publie pas.
                "causes": [refusal for refusal in refusals
                           if "{" not in refusal
                           and refusal.startswith(("steps", "'steps'", "acceptance",
                                                   "common_errors"))][:6],
            },
        ],
        "acceptance": [
            "Every step declares one of target, command or tool",
            "The action of every step is one of: " + ", ".join(actions),
            "Steps are numbered from 1, without a gap or a repeat",
            "Each entry of common_errors carries at least one cause",
        ],
        "lines": [
            "The validator checks in this order, and a Skill written in another "
            "order fails at the last gate rather than the first:",
            "",
            "1. `name` — non-empty, unique per project (a second one answers 409)",
            "2. `description` — the only other column the search compares, "
            "with `name` and `body_md`",
            "3. `tool_bindings` — the MCP tools, ordered by step",
            "4. `mdm_tags` — structured, read by search (AI-154, commit 7dbb64a2)",
            "5. `steps` — the sequence",
            "6. `acceptance` — the criteria, for the whole Skill",
            "7. `common_errors` — symptom, then its causes",
            "",
            f"**Bounds, read from the validator:** at most {limits.get('STEPS', '?')} "
            f"steps, criteria or errors; a label at most {limits.get('LABEL', '?')} "
            f"characters; any other text at most {limits.get('TEXT', '?')}.",
            "",
            f"**The closed action vocabulary:** {', '.join('`' + a + '`' for a in actions)}. "
            "It decides the icon the console draws, so a seventh value would break "
            "the rendering without anything reporting it.",
        ],
        "note": (
            "The rules below are not advice: they are the refusals the product "
            "raises. A Skill that violates one is not accepted at all."
        ),
    }

    knowledge = {
        "title": "Skill authoring — every rule the validator enforces",
        "trade": "skill-editor",
        "body_md": "\n".join([
            "Every line below is a refusal raised by "
            "`core.context_store.validate_procedure_frontmatter`. Nothing here is "
            "advice, and nothing is written by hand: a rule that stops being "
            "enforced disappears from this page the next time it is regenerated.",
            "",
            f"**{len(refusals)} refusals**, in the order the validator raises them:",
            "",
            *[f"- {refusal}" for refusal in refusals],
            "",
            "## The two levels",
            "",
            "| Level | Keys |",
            "| --- | --- |",
            "| Skill | `name`, `description`, `mdm_tags`, `acceptance`, `common_errors` |",
            "| Step | `step`, `action`, `label`, `target`, `command`, `tool`, `stop_if` |",
            "",
            "`target`, `command` and `tool` are three different things — what is "
            "acted on, a command to run, and an MCP tool in the glossary's strict "
            "sense. **At least one of them is required on every step**: an action "
            "and a label with nothing acted on is the vague step a console cannot "
            "display.",
        ]),
    }
    return skill, knowledge


def operation_skills() -> list[dict]:
    """Une Skill « how to » par operation declaree -- derivee des routes reelles."""
    built = []
    for name, operation in DECLARATION["operations"].items():
        owners = [slug for slug, trade in DECLARATION["trades"].items()
                  if name in trade.get("operations", [])]
        if not owners:
            continue
        lines: list[str] = []
        for endpoint in operation["endpoints"]:
            facts = _endpoint_facts(operation["module"], endpoint)
            if facts is None:
                lines.append(f"- ⚠️ `{endpoint}` declares no route in "
                             f"`{operation['module']}.py` — the declaration is stale.")
                continue
            for methods, route in facts["routes"]:
                lines.append(f"- **{methods} `{route}`**")
            if facts["scope"]:
                lines.append(f"  - project scope: {facts['scope']}")
            if facts["keys"]:
                lines.append("  - body fields read by the handler: "
                             + ", ".join(f"`{key}`" for key in facts["keys"]))
        built.append({
            "trade": owners[0],
            "kind": "how-to",
            "name": operation["label"],
            "description": "The routes, the fields, and where the project scope goes.",
            "source": f"server/core/{operation['module']}.py — read from the handlers",
            "lines": lines,
            "note": operation["note"],
        })
    return built


def skills() -> list[dict]:
    """Toutes les Skills du corpus. Chacune porte sa source ; aucune n'est redigee."""
    built: list[dict] = list(role_skills()) + list(operation_skills())
    built.append(skill_authoring()[0])

    # 1. LA SEULE PROCEDURE RATIFIEE DU DEPOT.
    steps = _setup_steps()
    if steps:
        built.append({
            "trade": "data-analyst",
            "kind": "ratified-procedure",
            "name": "Set up a Datastream end to end",
            "description": (
                f"The {len(steps)} ratified steps, from source to schedule. "
                "The only procedure this repository writes as such."
            ),
            "source": ("docs/product-architecture/SURFACE-STATE.md, derived from "
                       "docs/product-architecture/datastream-workbench-and-wizard.md"),
            "lines": [
                f"{index}. **{label}** — state `{state}`  \n   delivered by: {carrier}"
                for index, (label, state, carrier) in enumerate(steps, 1)
            ],
            "note": (
                "An `OK` means *built, routed, covered* — not *walked through*. And the "
                "confirmation step engraves plan versions that can never be deleted: a "
                "trial run belongs on a local database, or nowhere."
            ),
        })

    # 2. LE CRITERE D'ACCEPTATION DE CHAQUE SURFACE, VERBATIM.
    for path in sorted(list(DOCS.glob("*.md")) + list((DOCS / "capabilities").glob("*.md"))):
        criteria = incomplete_if(path)
        if not criteria:
            continue
        built.append({
            "trade": TRADE_OF_DOCUMENT.get(path.stem, FALLBACK_TRADE),
            "kind": "acceptance-criteria",
            "name": f"Judge whether {path.stem} is finished",
            "description": (
                f"Test, prove and accept {path.stem}: the {len(criteria)} ratified "
                "acceptance criteria, verbatim. A surface is judged on these, not on "
                "green tests."
            ),
            "source": f"docs/product-architecture/{path.relative_to(DOCS).as_posix()}",
            # Les criteres vont AUSSI dans la cle structuree : dans le corps seul,
            # ils se lisent mais ne se comptent pas, et l'interface n'a rien a rendre.
            "acceptance": criteria,
            "lines": [f"- {item};" for item in criteria],
            "note": (
                "These are INCOMPLETENESS criteria: if one of them holds, the surface is "
                "not finished — even with every test green. The state of each criterion "
                "lives in `completeness-ledger.json`."
            ),
        })

    # 3. LE METIER QUI FABRIQUE LES AUTRES. Ses regles sont CITEES, pas deduites.
    built.append({
        "trade": "business-writer",
        "kind": "decomposition-rule",
        "name": "Decompose a trade into Skills and Knowledge",
        "description": "The shape the ratified target imposes, quoted word for word.",
        "source": ("docs/product-architecture/context-hub.md "
                   "docs/product-architecture/glossary.md "
                   "docs/product-architecture/governance.md"),
        "lines": [
            "1. **The root is an existing Business Domain.** `governance.md`: "
            "“Business Domains are editable organization-owned roots, not six fixed "
            "departments.” Six are seeded into every organization; creating more "
            "beside them puts two unrelated vocabularies side by side.",
            "2. **A trade is a classification, not a root.** `context-hub.md`: "
            "“A mindmap rooted in the project's Business Domains. It expands through "
            "**classification layers** to products, activities, metrics, views, "
            "Datastreams, knowledge and Skills.”",
            "3. **A Skill is grouped by classification.** `context-hub.md`: "
            "“Skills Registry — Versioned procedures grouped by governed business "
            "classification, not six hardcoded departments. Skills may bind tools, "
            "metrics, views and evidence requirements.”",
            "4. **Knowledge is not a Skill.** `context-hub.md`: “A knowledge item is "
            "not required to be a Skill, but it must be linkable to one or more governed "
            "business keys.” One states what the business MEANS, the other what to DO.",
            "5. **A Skill is not a Tool.** `glossary.md`: a Tool is an MCP tool. A Skill "
            "*binds* tools; described as one, it loses the version and the evidence "
            "requirements that make it governable.",
            "6. **A classification is not a copy.** `governance.md`: “A classification "
            "organizes objects; it does not create a second copy of them.”",
        ],
        "note": (
            "*Procedure* is not a product noun: `glossary.md` files it among the delivered "
            "tokens. The object is called a **Skill**."
        ),
    })

    built.append({
        "trade": "business-writer",
        "kind": "decomposition-rule",
        "name": "Know what the documentation does NOT say",
        "description": "Where a trade cannot fend for itself, and why.",
        "source": "docs/product-architecture/SURFACE-STATE.md",
        "lines": _missing_lines(),
        "note": (
            "These gaps are projected AS gaps. Filling them with prose would produce a "
            "base that looks complete and lies — the defect being repaired, not one to add."
        ),
    })

    # 4. LA FICHE PRODUIT. Le seul artefact que ce depot demande d'ECRIRE :
    #    ses etapes et ses criteres sont lus dans product-sheet.md, pas rediges.
    sheet_steps = _product_sheet_steps()
    if sheet_steps:
        built.append({
            "trade": "business-writer",
            "kind": "ratified-procedure",
            "name": "Write a toorow product sheet",
            "description": (
                f"The {len(sheet_steps)} ratified steps, from the governed read to the "
                "published Knowledge card. A sheet cites the Hub, never the writer's memory."
            ),
            "source": "docs/product-architecture/product-sheet.md",
            "steps": sheet_steps,
            "acceptance": incomplete_if(DOCS / "product-sheet.md"),
            "lines": [
                f"{step['step']}. **{step['label']}** — `{step['action']}` on "
                f"{step['target']}"
                for step in sheet_steps
            ],
            "note": (
                "Seven mandatory sections, in order: Problem, Audience, Value proposition, "
                "Key capabilities, Governed metrics, Honest limits, Context links. The "
                "sheet is published as a Knowledge card linked to a governed business key."
            ),
        })
    return built


def _missing_lines() -> list[str]:
    """Les surfaces dont personne ne peut dire « ai-je tout ce qu'il faut ? »."""
    page = _read(DOCS / "SURFACE-STATE.md")
    match = re.search(r"^##\s*Surfaces sans table d'etapes\s*$(.+?)(?=^##\s|\Z)",
                      page, re.M | re.S)
    names = re.findall(r"^-\s*`(.+?)`", match.group(1), re.M) if match else []
    return [
        "`SURFACE-STATE.md`: their target is written, but nobody can answer “do I have "
        "everything I need?” without re-reading the code. That is the work remaining.",
        "",
        f"**{len(names)} surfaces carry no step table at all:**",
        *[f"- `{name}`" for name in names],
        "",
        "Exactly one surface carries a ratified procedure: "
        "`datastream-workbench-and-wizard.md`. Any other “procedure” appearing here "
        "would have been invented.",
    ]


# ---------------------------------------------------------------------------
# La matiere -- une carte par objet reel, composee, jamais redigee
# ---------------------------------------------------------------------------


def connector_cards() -> list[dict]:
    """Une carte par connecteur : profils de report, dimensions, sources.

    Reponse litterale a « le nom des dimensions des reports, le detail des
    sources ». Tout vient de `manifest.json` et `api_catalog.json`, generes.
    """
    cards = []
    for directory in sorted(p for p in MODULES.iterdir() if p.is_dir()):
        manifest_path = directory / "manifest.json"
        if not manifest_path.exists():
            continue
        manifest = json.loads(_read(manifest_path))
        catalog_path = directory / "api_catalog.json"
        catalog = json.loads(_read(catalog_path)) if catalog_path.exists() else {}
        fields = catalog.get("fields", [])

        by_kind: dict[str, int] = {}
        by_tier: dict[str, int] = {}
        for field in fields:
            by_kind[field.get("kind", "?")] = by_kind.get(field.get("kind", "?"), 0) + 1
            by_tier[field.get("tier", "?")] = by_tier.get(field.get("tier", "?"), 0) + 1

        lines = [
            f"**{manifest.get('display_name', directory.name)}** — connector "
            f"`{directory.name}`, authentication `{manifest.get('auth_type', '?')}`.",
            "",
            f"Catalog `{catalog.get('api_version', '—')}`, captured on "
            f"`{catalog.get('generated_at', '—')}`: **{len(fields)} fields** — "
            + ", ".join(f"{count} {kind}" for kind, count in sorted(by_kind.items()))
            + ".",
            "",
            "## Report profiles — what can actually be requested",
            "",
        ]
        profiles = manifest.get("report_profiles") or []
        if profiles:
            lines += ["| Profile | Dimensions | Metrics |", "| --- | --- | --- |"]
            for profile in profiles:
                dims = ", ".join(f"`{d}`" for d in (profile.get("dimensions") or [])) or "—"
                mets = ", ".join(f"`{m}`" for m in (profile.get("metrics") or [])) or "—"
                lines.append(
                    f"| **{profile.get('display_name', profile.get('id'))}** "
                    f"(`{profile.get('id')}`) | {dims} | {mets} |"
                )
        else:
            lines.append("_No report profile declared._")

        for label, key in (
            ("dimensions", "canonical_dimension_mapping"),
            ("metrics", "canonical_metric_mapping"),
            ("events", "canonical_event_mapping"),
        ):
            mapping = manifest.get(key) or {}
            if not isinstance(mapping, dict) or not mapping:
                continue
            lines += ["", f"## Into the canonical vocabulary — {len(mapping)} {label}", ""]
            lines += [f"- `{source}` → **{target}**" for source, target in sorted(mapping.items())]

        if by_tier:
            lines += [
                "",
                "## Field tiering",
                "",
                ", ".join(f"**{tier}**: {count}" for tier, count in sorted(by_tier.items())),
                "",
                "A `standard` field is guaranteed; the rest is not. `exposure: planned` "
                "means catalogued but not yet served.",
            ]

        sources = catalog.get("sources") or []
        if sources:
            lines += ["", "## Official sources behind this catalog", ""]
            lines += [
                f"- {s.get('kind', 'source')} — {s.get('url', '')} "
                f"(captured {s.get('fetched_at', '—')})"
                for s in sources
            ]

        cards.append({
            "title": f"Connector — {manifest.get('display_name', directory.name)}",
            "body_md": "\n".join(lines),
            "trade": "data-analyst",
        })
    return cards


def capability_cards() -> list[dict]:
    """Les capacites transverses, EN ENTIER : la reponse a « ou je pose ca ? »."""
    return [
        {"title": f"Capability — {path.stem}", "body_md": _read(path),
         "trade": TRADE_OF_DOCUMENT.get(path.stem, FALLBACK_TRADE)}
        for path in sorted((DOCS / "capabilities").glob("*.md"))
        if path.stem != "README"
    ]


def architecture_cards() -> list[dict]:
    """Les documents ratifies, EN ENTIER. Ce sont EUX la cible, pas leur resume."""
    return [
        {
            "title": f"Architecture — {path.stem}",
            "body_md": _read(path),
            "trade": TRADE_OF_DOCUMENT.get(path.stem, FALLBACK_TRADE),
        }
        for path in sorted(DOCS.glob("*.md"))
        if path.name != "SURFACE-STATE.md"
    ]


def reference_cards() -> list[dict]:
    """Les quatre index generes -- l'outillage, la base, les surfaces, les documents."""
    wanted = [
        ("TOOLBOX", "Tooling — what this repository can do, and what each tool requires",
         ROOT / "TOOLBOX.md"),
        ("SCHEMA", "Database structure — what a table requires, and who owns it",
         ROOT / "SCHEMA.md"),
        ("SURFACE-STATE", "Surface state — the steps, and whether we have them",
         DOCS / "SURFACE-STATE.md"),
        ("INDEX", "Document index — and what has been superseded",
         ROOT / "_bmad-output" / "INDEX.md"),
    ]
    # Un index peut interesser DEUX metiers -- `SCHEMA` sert au developpeur comme
    # au DevOps. On le projette une fois, rattache au premier qui le declare : un
    # objet en double est un objet dont les deux copies divergent.
    return [
        {"title": title, "body_md": _read(path),
         "trade": _trade_of_reference(key) or FALLBACK_TRADE}
        for key, title, path in wanted
        if path.exists()
    ]


def screen_cards() -> list[dict]:
    """Une carte par ecran, AVEC ses attentes -- pas leur nombre.

    La premiere version publiait « Attentes declarees : 4 ». Un compte ne dit pas
    ce qui est attendu : c'etait exactement le defaut nomme.
    """
    # LE CACHE EST LU PAR SON PROPRIETAIRE. `screens/board.json` est un cache de
    # `screens.py resolve()`, et lui seul sait quand il a vieilli : la garde vit
    # dans `screens.load_board()` depuis le 2026-08-21. Le lire par son chemin,
    # comme ce fichier le faisait, publiait un board perime dans le Context Hub.
    from dataclasses import asdict  # noqa: PLC0415

    from screens import load_board  # noqa: PLC0415

    board = {"screens": [asdict(screen) for screen in load_board()]}
    declared = json.loads(_read(ROOT / "screens" / "expectations.json"))
    cards = []
    for screen in board["screens"]:
        entry = declared.get(screen["id"]) or {}
        expects = entry.get("expects", [])
        lines = [
            entry.get("mission", "") or "_No mission declared in expectations.json._",
            "",
            f"- Identifier: `{screen['id']}`",
            f"- Route: `{screen.get('route', '') or '—'}`",
            f"- Mounted: {'yes' if screen.get('mounted_at') else 'NO'}",
        ]
        if expects:
            lines += ["", "## What this page is expected to do", "",
                      "| Expectation | Proof | Value |", "| --- | --- | --- |"]
            lines += [
                f"| {e.get('what', '')} | `{e.get('proof', '')}` | "
                f"{('`' + e['value'] + '`') if e.get('value') else '—'} |"
                for e in expects
            ]
            lines += ["", "An unmet expectation is not an error: it is the work remaining. "
                          "The resolved state lives in `screens/expectations.md`."]
        else:
            lines += ["", "⚠️ **No expectation declared.** Nobody can therefore say whether "
                          "this page is finished — that is the first gap to close."]
        cards.append({
            "title": f"Screen — {screen['title']}",
            "body_md": "\n".join(lines),
            "trade": _trades_with("screens")[0] if _trades_with("screens") else FALLBACK_TRADE,
        })
    return cards


def knowledge() -> list[dict]:
    """La matiere projetee.

    ⚠️ SANS LES CONNECTEURS, ET C'EST VOULU. `core/context_seed.py` les seme
    deja -- en process, a la creation du projet et apres chaque atterrissage --
    une carte PLATEFORME par module BRANCHE au projet, composee des memes
    fichiers verbatim, idempotente par index unique, et qui refuse d'ecraser une
    edition humaine. Mesure : mon `Connector — X` se serait pose a cote de son
    `Connector: X`. Deux copies qui divergent, exactement ce que
    `governance.md` refuse.
    """
    return (
        [skill_authoring()[1]]
        + capability_cards()
        + architecture_cards()
        + reference_cards()
        + screen_cards()
    )


def edges() -> tuple[list[tuple[str, str]], list[str]]:
    """(carte, Skill) quand la correspondance est DERIVABLE, et le reste.

    Deux regles, toutes deux mecaniques -- aucune n'est choisie carte par carte :

      * une carte d'Architecture ou de Capacite se relie a la Skill qui juge
        LE MEME document : « Architecture — governance » <-> « Juger si
        "governance" est fini ». C'est le document et son critere d'acceptation ;
      * une carte de Connecteur se relie a la procedure de montage : l'etape 2
        ratifiee est « Select a usable Source Account », et c'est exactement ce
        qu'un connecteur fournit.

    Le RESTE est rendu tel quel, sans arete. Une carte sans Skill est un manque
    mesure -- lui en inventer une remettrait de la prose la ou Jean vient de dire
    qu'il n'en veut pas.
    """
    skill_names = {skill["name"] for skill in skills()}
    pairs: list[tuple[str, str]] = []
    orphans: list[str] = []
    for card in knowledge():
        family, _, stem = card["title"].partition(" — ")
        judge = f"Judge whether {stem} is finished"
        if family in {"Architecture", "Capability"} and judge in skill_names:
            pairs.append((card["title"], judge))
        elif family == "Connector" and "Set up a Datastream end to end" in skill_names:
            pairs.append((card["title"], "Set up a Datastream end to end"))
        else:
            orphans.append(card["title"])
    return pairs, orphans


if __name__ == "__main__":
    all_skills, cards = skills(), knowledge()
    print(f"{len(TRADES)} métiers, {len(all_skills)} Skills, {len(cards)} Knowledge")
    for slug, trade in TRADES.items():
        s = sum(1 for x in all_skills if x["trade"] == slug)
        k = sum(1 for x in cards if x["trade"] == slug)
        print(f"  {slug:<18} racine `{trade['root']:<12}` {s:>3} Skills  {k:>3} Knowledge")
    families: dict[str, int] = {}
    for card in cards:
        families[card["title"].split(" — ")[0]] = families.get(
            card["title"].split(" — ")[0], 0) + 1
    ordered = sorted(families.items(), key=lambda kv: -kv[1])
    print("\n  " + "  ".join(f"{f}:{c}" for f, c in ordered))
    print(f"\n{sum(len(c['body_md']) for c in cards) // 1024} Ko de matière")
