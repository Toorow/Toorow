#!/usr/bin/env python3
r"""Une Skill qui cite ce que le depot n'a plus -- le dire, par Skill.

POURQUOI. Jean, 2026-08-03 : « imagine demain le document existe plus. Cas
pratique : ben ca donne un retour sur le skill disant "ben ca, on peut pas le
verifier". »

C'est la seule forme de retour sur une Skill qui ne demande a personne d'ecrire
quoi que ce soit : le DEPOT le dit. Une Skill dont la source a disparu, dont une
cible ne resout plus, ou dont une commande appelle un script supprime, est une
Skill qui envoie quelqu'un vers du vide -- et c'est exactement le defaut que
CLAUDE.md section 3 nomme : « un document qui decrit un etat revolu est pire que
pas de document : il est cru ».

CE QU'IL VERIFIE, et rien d'autre :

  * la SOURCE de chaque Skill -- le fichier dont elle est derivee ;
  * chaque `target` de pas qui ressemble a un chemin ;
  * chaque `command` qui appelle un fichier du depot (`python scripts/x.py`,
    `uv run python e2e/runner.py`).

  * chaque `tool` de pas -- l'outil MCP existe-t-il dans ce deploiement, et
    est-il declare par UN SEUL pas.

POURQUOI LE SECOND. `skill_steps.observed_step` rend `None` quand un nom d'outil
correspond a plusieurs pas (`len(candidates) != 1`) : la regle du document est
qu'une correspondance ambigue n'enregistre RIEN. Une Skill qui declare le meme
outil a deux pas n'a donc aucun de ces deux pas observable -- elle a l'air suivie
et ne l'est jamais. C'est invisible a la lecture, et mesurable ici.

CE QU'IL NE VERIFIE PAS, et le dit plutot que de le laisser croire : qu'une
commande REUSSIT, qu'un outil soit VISIBLE pour un appelant donne -- la
visibilite depend du profil de capacite et de l'identite, pas du depot -- ou
qu'un chemin de frontmatter (`frontmatter \`name\``) designe quoi que ce soit.
Une porte qui pretend plus qu'elle ne mesure est une porte qu'on apprend a
ignorer.

    python scripts/check_skill_targets.py            # le rapport, par Skill
    python scripts/check_skill_targets.py --gate     # non-zero si une citation est morte
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from working_context_corpus import knowledge, skills  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

#: Ce qui, dans une commande, designe un fichier du depot. Un chemin cite dans
#: une commande est aussi fragile qu'un chemin cite ailleurs.
_IN_COMMAND = re.compile(r"(?:^|\s)((?:scripts|e2e|server|ui|docs|infra)/[\w./-]+\.\w+)")


def _looks_like_path(value: str) -> bool:
    """Un chemin, pas une reference de frontmatter ni une phrase."""
    if "`" in value or " " in value.strip():
        return False
    return "/" in value or value.endswith((".md", ".py", ".json", ".tsx", ".ts", ".sql"))


def _citations(skill: dict) -> list[tuple[str, str]]:
    """(d'ou vient la citation, le chemin cite) -- sans doublon, dans l'ordre."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(origin: str, value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            found.append((origin, value))

    # ⚠️ NE PAS RETIRER LE POINT DE TETE. Premiere version : `.strip("`.,")`
    # transformait `.claude/agents/x.md` en `claude/agents/x.md`, et la porte
    # annoncait six citations mortes qui existaient toutes. Une porte qui accuse
    # a tort est pire qu'une porte absente -- on apprend a l'ignorer.
    #
    # Et un jeton SANS repertoire (`glossary.md` dans « a/b.md + glossary.md »)
    # n'est pas verifiable tel quel : il est ignore ici, et la source du corpus
    # doit porter des chemins complets. Deviner son repertoire reviendrait a
    # inventer la citation qu'on pretend controler.
    for token in re.split(r"[\s,+()]+", skill.get("source", "")):
        cleaned = token.strip("`,").rstrip(".")
        if cleaned and "/" in cleaned and _looks_like_path(cleaned):
            add("source", cleaned)

    for step in skill.get("steps", []):
        target = step.get("target", "")
        if _looks_like_path(target):
            add(f"step {step['step']} target", target)
        for match in _IN_COMMAND.finditer(step.get("command", "")):
            add(f"step {step['step']} command", match.group(1))
    return found



def registered_tools() -> set[str]:
    """Les outils que ce depot enregistre, lus du code qui les enregistre.

    La source est `register_profiled(mcp, <handler>, ...)` : le catalogue vit
    dans le code, jamais dans une table, et une liste tenue a la main ici serait
    une deuxieme verite qui divergerait de la premiere.
    """
    names: set[str] = set()
    pattern = re.compile(r"register_profiled\(\s*mcp,\s*([a-z_][a-z0-9_]*)")
    for path in (ROOT / "server" / "core").glob("*.py"):
        for match in pattern.finditer(path.read_text(encoding="utf-8", errors="replace")):
            names.add(match.group(1))
    # `handler` est le nom d'une variable locale dans une fabrique, pas un outil.
    names.discard("handler")
    return names


def _tool_faults(skill: dict, known: set[str]) -> list[tuple[str, str]]:
    """(d'ou vient la faute, ce qui ne va pas) -- par Skill."""
    faults: list[tuple[str, str]] = []
    declared: dict[str, list[int]] = {}
    for step in skill.get("steps", []):
        tool = str(step.get("tool") or "").strip()
        if not tool:
            continue
        declared.setdefault(tool, []).append(step["step"])
        if known and tool not in known:
            faults.append((f"step {step['step']} tool", f"{tool} -- no such tool in this deployment"))
    for tool, steps in declared.items():
        if len(steps) > 1:
            faults.append((
                "steps " + ", ".join(str(s) for s in steps),
                f"{tool} -- declared by {len(steps)} steps, so none of them is ever observed",
            ))
    bound = {str(b.get("tool") or "").strip() for b in skill.get("tool_bindings", [])}
    for tool in sorted(bound - set(declared)):
        if tool:
            faults.append(("tool_bindings", f"{tool} -- bound to no step that declares it"))
    return faults


def _resolves(value: str) -> bool:
    """Un chemin resout s'il existe -- fichier ou repertoire."""
    return (ROOT / value).exists()


def report() -> tuple[int, int, list[tuple[str, str, str]]]:
    dead: list[tuple[str, str, str]] = []
    checked = 0
    known = registered_tools()
    for skill in skills():
        for origin, value in _citations(skill):
            checked += 1
            if not _resolves(value):
                dead.append((skill["name"], origin, value))
        for origin, fault in _tool_faults(skill, known):
            checked += 1
            dead.append((skill["name"], origin, fault))
    return len(skills()), checked, dead


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gate", action="store_true",
                    help="sortir non-zero des qu'une citation ne resout plus")
    args = ap.parse_args()

    total, checked, dead = report()
    cards = len(knowledge())
    print(f"{total} Skills, {cards} Knowledge — {checked} citations résolues contre le dépôt")

    if not dead:
        print("aucune citation morte")
        return 0

    print(f"\n{len(dead)} citation(s) que le dépôt n'a plus — "
          "chacune est un retour sur sa Skill :")
    for name, origin, value in dead:
        print(f"  « {name} »")
        print(f"      {origin} → {value}  ⟂ CE PAS N'EST PLUS VÉRIFIABLE")
    print("\nUne Skill qui cite ce qui n'existe plus envoie quelqu'un vers du vide.")
    return 1 if args.gate else 0


if __name__ == "__main__":
    raise SystemExit(main())
