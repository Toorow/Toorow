"""The console's OWN address table, read from the navigation registry.

WHY THIS EXISTS. A server payload that names a repair address names it for one
reader: `ui/admin/src/shell/ownerResolution.ts`, which resolves an owner
reference against `shell/navigation/<workspace>.ts` and REFUSES anything the
registry does not declare. A token outside that vocabulary -- `"surface":
"dimension_conformance"` -- is not refused loudly either: it is not even an
owner reference, so no console code path ever looks at it. The alert renders
with a repair address nothing can open.

A Python test cannot import the resolver, and RESTATING its table here would be
the defect this module exists against -- an instrument measuring its own copy.
So the table is PARSED from the registry files themselves, through
`navigation_source`'s sibling reader, and the parse is asserted to be non-empty
by every caller: a parser that silently returns nothing turns a red guard green.

The resolution rules are `resolveOwnerReference`'s, in its order:

  * `surface` is `project` or `global`, and nothing else;
  * a global reference names a declared settings area and one of its sections;
  * a project reference names a declared workspace AND a declared section of it;
  * an object reference carries both a type and an id, the type is a contract
    that section declares, and a `tab` is one the contract lists;
  * a `tab` or a `version_id` without an object is refused;
  * a bare `action` must be declared by the collection;
  * an unknown `lens` is DROPPED rather than refused -- the section still
    resolves -- so it is reported as a warning, never as a failure.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field

_UI_SHELL = pathlib.Path(__file__).resolve().parents[3] / "ui" / "admin" / "src" / "shell"

#: `ownerResolution.ts#GLOBAL_OWNER_SECTIONS`. The global surfaces are declared
#: in the resolver rather than in the workspace registry, so this is where they
#: are read from -- the same file, parsed, never retyped.
_GLOBAL_TABLE_FILE = "ownerResolution.ts"


def _strip_comments(source: str) -> str:
    """Remove `//` and `/* */` comments without touching string literals.

    Bracket matching over the raw text is wrong here: these files carry long
    prose comments containing `(`, `[` and unbalanced quotes, and one of them
    would swallow the rest of a workspace.
    """
    out: list[str] = []
    index = 0
    length = len(source)
    quote: str | None = None
    while index < length:
        char = source[index]
        if quote is not None:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(source[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "\"'`":
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and source[index + 1] == "/":
            while index < length and source[index] != "\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and source[index + 1] == "*":
            index += 2
            while index + 1 < length and not (source[index] == "*" and source[index + 1] == "/"):
                index += 1
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _split_arguments(source: str, start: int) -> tuple[list[str], int]:
    """Split the top-level arguments of a call whose `(` sits at *start*."""
    depth = 0
    argument_start = start + 1
    arguments: list[str] = []
    index = start
    quote: str | None = None
    while index < len(source):
        char = source[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "\"'`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                arguments.append(source[argument_start:index])
                return arguments, index
        elif char == "," and depth == 1:
            arguments.append(source[argument_start:index])
            argument_start = index + 1
        index += 1
    raise ValueError("unterminated call in the navigation registry")


@dataclass(frozen=True)
class ObjectContract:
    type: str
    tabs: tuple[str, ...]


@dataclass
class Section:
    slug: str
    label: str
    objects: dict[str, ObjectContract] = field(default_factory=dict)
    actions: tuple[str, ...] = ()
    lenses: tuple[str, ...] = ()


def _resolve_aliases(source: str) -> dict[str, tuple[str, ...]]:
    """`const MASTER_DATA_TABS = [...]` and its siblings, as real lists."""
    aliases: dict[str, tuple[str, ...]] = {}
    for match in re.finditer(r"const\s+([A-Z][A-Z0-9_]*)\s*=\s*\[([^\]]*)\]", source):
        aliases[match.group(1)] = tuple(re.findall(r'"([^"]+)"', match.group(2)))
    return aliases


def _string_list(fragment: str, aliases: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    stripped = fragment.strip()
    if stripped in aliases:
        return aliases[stripped]
    return tuple(re.findall(r'"([^"]+)"', stripped))


def read_workspaces(shell: pathlib.Path | None = None) -> dict[str, dict[str, Section]]:
    """Every declared workspace, its sections, their objects, actions and lenses."""
    base = shell or _UI_SHELL
    # LES ALIAS SE RESOLVENT SUR TOUT LE REGISTRE, PAS FICHIER PAR FICHIER.
    # Depuis AD-42 le registre est en sept fichiers, et une liste d'onglets vit
    # la ou elle est partagee : `MASTER_DATA_TABS` est exporte par
    # `navigation/vocabulary.ts` et utilise par `navigation/governance.ts`.
    # Resolus fichier par fichier, les trois contrats de Master Data lisaient
    # ZERO onglet -- et tout controle d'onglet de gouvernance passait a
    # l'aveugle, sans qu'une ligne rougisse.
    sources = {
        path: _strip_comments(path.read_text(encoding="utf-8"))
        for path in sorted((base / "navigation").glob("*.ts"))
    }
    registry = base / "navigation.ts"
    if registry.exists():
        sources[registry] = _strip_comments(registry.read_text(encoding="utf-8"))
    aliases: dict[str, tuple[str, ...]] = {}
    for text in sources.values():
        aliases.update(_resolve_aliases(text))

    workspaces: dict[str, dict[str, Section]] = {}
    for source in sources.values():
        key_match = re.search(r'\bkey:\s*"([a-z-]+)"', source)
        if not key_match:
            continue
        sections: dict[str, Section] = {}
        for match in re.finditer(r"\bsection\(", source):
            arguments, _ = _split_arguments(source, match.end() - 1)
            slug = re.search(r'"([^"]*)"', arguments[0])
            label = re.search(r'"([^"]*)"', arguments[1]) if len(arguments) > 1 else None
            if not slug or not label:
                continue
            objects: dict[str, ObjectContract] = {}
            if len(arguments) > 2:
                # `tabs:` DOES NOT HAVE TO FOLLOW `type:` IMMEDIATELY, and the day
                # it stopped doing so this parser read EVERY object contract as
                # zero tabs. Measured 2026-09-05: a `label:` added between the
                # two keys of every contract of every workspace made
                # `ObjectContract(type='datastream', tabs=())`, and three
                # conformance tests went red saying a DQ alert names a repair
                # address the console cannot open -- `'datastream' declares no
                # tab 'mapping' (it has [])` -- while `navigation/data.ts`
                # declared eight. The order of the keys of a TypeScript object
                # literal is not a contract; the object is. `[^}]*?` cannot cross
                # the closing brace, so `tabs:` is still bound to the SAME
                # contract and never borrowed from the next one.
                for found in re.finditer(
                    r'type:\s*"([a-z-]+)"\s*,(?:[^}]*?\btabs:\s*([A-Z_]+|\[[^\]]*\]))?',
                    arguments[2],
                ):
                    objects[found.group(1)] = ObjectContract(
                        type=found.group(1),
                        tabs=_string_list(found.group(2) or "", aliases),
                    )
            sections[slug.group(1)] = Section(
                slug=slug.group(1),
                label=label.group(1),
                objects=objects,
                actions=_string_list(arguments[3], aliases) if len(arguments) > 3 else (),
                lenses=tuple(re.findall(r'slug:\s*"([a-z-]+)"', arguments[4]))
                if len(arguments) > 4
                else (),
            )
        workspaces[key_match.group(1)] = sections
    return workspaces


def read_global_sections(shell: pathlib.Path | None = None) -> dict[str, tuple[str, ...]]:
    """`GLOBAL_OWNER_SECTIONS`, read from the resolver that enforces it."""
    base = shell or _UI_SHELL
    source = _strip_comments((base / _GLOBAL_TABLE_FILE).read_text(encoding="utf-8"))
    match = re.search(
        r"GLOBAL_OWNER_SECTIONS:\s*Record<string,\s*readonly string\[\]>\s*=\s*\{", source
    )
    if not match:
        return {}
    body, _ = _split_arguments(source, match.end() - 1)
    joined = ",".join(body)
    table: dict[str, tuple[str, ...]] = {}
    for found in re.finditer(r'"([a-z-]+)":\s*\[([^\]]*)\]', joined):
        table[found.group(1)] = tuple(re.findall(r'"([^"]+)"', found.group(2)))
    return table


def resolve_console_address(
    reference: object, shell: pathlib.Path | None = None
) -> str | None:
    """`None` when the console can open it; otherwise WHY it cannot, in one line.

    The refusal codes are `ownerResolution.ts`'s own -- prefixed by the reason a
    Python reader needs, since this is read in a pytest failure, not by a person
    in a browser.
    """
    workspaces = read_workspaces(shell)
    if not workspaces:
        raise AssertionError(
            "the navigation registry parsed to zero workspaces -- this guard has "
            "gone blind, which is worse than absent"
        )
    if not isinstance(reference, dict):
        return "not an owner reference at all (the console reads a mapping)"
    surface = reference.get("surface")
    if surface == "global":
        table = read_global_sections(shell)
        if not table:
            raise AssertionError("GLOBAL_OWNER_SECTIONS parsed to nothing -- guard blind")
        allowed = table.get(str(reference.get("global_surface") or ""))
        if not allowed or str(reference.get("global_section") or "") not in allowed:
            return (
                f"unknown_settings_area: {reference.get('global_surface')!r} > "
                f"{reference.get('global_section')!r} is not a settings area the console has"
            )
        return None
    if surface != "project":
        return (
            f"unknown_surface: {surface!r} is not a console surface. The console "
            "knows two: 'project' and 'global'."
        )
    workspace = str(reference.get("workspace") or "")
    section_slug = str(reference.get("section") or "")
    sections = workspaces.get(workspace)
    if sections is None:
        return f"unknown_section: no workspace {workspace!r} is declared"
    section = sections.get(section_slug)
    if section is None:
        return f"unknown_section: {workspace!r} declares no section {section_slug!r}"
    object_type = reference.get("object_type")
    object_id = reference.get("object_id")
    tab = reference.get("tab")
    if object_type or object_id:
        if not object_type or not object_id:
            return "incomplete_object: an object reference carries a type AND an id"
        contract = section.objects.get(str(object_type))
        if contract is None:
            return (
                f"unknown_object_kind: {workspace}>{section_slug} holds no "
                f"{object_type!r} contract"
            )
        if tab and str(tab) not in contract.tabs:
            return (
                f"unknown_object_view: {object_type!r} declares no tab {tab!r} "
                f"(it has {list(contract.tabs)})"
            )
    elif tab or reference.get("version_id"):
        return "stray_object_detail: a tab without the item it belongs to"
    else:
        action = reference.get("action")
        if action and str(action) not in section.actions:
            return f"unknown_section_action: {section_slug!r} offers no {action!r}"
    lens = reference.get("lens")
    if lens and not object_type and str(lens) not in section.lenses:
        # `resolveOwnerReference` DROPS an unknown lens rather than refusing, so
        # the address still opens -- on the section's default collection, which
        # is a different reading from the one that was named.
        return (
            f"dropped_lens: {section_slug!r} declares no lens {lens!r}, so the "
            "reader lands on the section default instead of the named reading"
        )
    return None
