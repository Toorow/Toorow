"""Conformance — la documentation PUBLIEE dit vrai sur le produit.

POURQUOI CE FICHIER EXISTE. `test_product_vocabulary.py` garde le vocabulaire
canonique dans `server/core/` et `ui/admin/src/shell/pages/`. Il ne regarde PAS
`docs/*.mdx`, c'est-a-dire les pages que Mintlify publie. Mesure du 2026-08-03,
avant ce fichier :

  - la page catalogue annoncait « 37 Built-in Modules » pour **39** connecteurs ;
  - elle en omettait **trois** (`bigquery`, `google-business-profile`,
    `instagram-insights`) ;
  - elle en annoncait **un qui n'existe pas** : Mailgun, qui n'est pas une source
    mais le TRANSPORT entrant (`server/inbound/adapters/mailgun.py`). La page
    promettait « transactional email deliveries, drops, opens, clicks » qu'aucun
    module ne peut livrer ;
  - le tutoriel « Add a connector » est bati de bout en bout sur
    `server/modules/mailgun/`, un repertoire qui n'existe pas ;
  - le nom retire `module` servait de nom de produit dans huit pages.

Un lecteur ne peut pas distinguer une page a jour d'une page perimee : les deux
se lisent pareil. Seule une comparaison au registre le peut.

CE QUI EST VERIFIE, ET CE QUI NE L'EST PAS. Le catalogue, les comptes, les
chemins de module cites, l'integrite de la navigation Mintlify, et le nom de
produit dans la COPIE. Pas la prose technique : le glossaire garde `module`
comme nom sur disque, donc `server/modules/<x>` et `module_kind` sont legitimes
et restent. Le changelog non plus : une note de version dit ce qui etait vrai a
sa date, la reecrire serait falsifier une histoire.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[3]
_DOCS = _REPO_ROOT / "docs"

#: The internal decision codes a published page may not cite. ONE constant,
#: read by the guard AND by its probe: a probe compiled against its own copy
#: of the pattern proves that copy works and says nothing about the guard.
_INTERNAL_CODE = (
    r"\bAD-\d+\b|\bCAP-\d+\b|\bNFR\d+\b|\bAI-\d+\b"
)
_MODULES = _REPO_ROOT / "server" / "modules"
_REGISTRY = _REPO_ROOT / "web" / "src" / "generated" / "connector-registry.json"
_CATALOG = _DOCS / "supported-connectors.mdx"

#: Une note de version est datee : elle dit ce qui etait vrai ce jour-la.
_HISTORICAL = {"changelog.mdx"}

#: Le nom d'un outil, ecrit comme un appel : `nom(` en debut de ligne (bloc de
#: code) ou juste apres un backtick (prose), plus les titres `### `nom``.
_TOOL_HEADING = re.compile(r"^### `([a-z][a-z0-9_]*)`", re.M)
_TOOL_CALL = re.compile(r"(?:^|`)([a-z][a-z0-9_]*)\(", re.M)

#: `module` reste legitime quand il designe le chemin sur disque, le champ de
#: manifeste, un terme d'outillage tiers, ou le nom d'une decision ratifiee.
_LEGITIMATE = (
    "server/modules",
    "module_kind",
    "module_name",
    "module contract",
    "Hot Module Replacement",
    "Module Auto-Discovery",
    "--module-path",
    ".py",  # une ligne qui cite un fichier Python parle du module PYTHON
)

_RETIRED = re.compile(r"\b[Mm]odules?\b|\b[Ee]xtensions?\b")


def _registry_ids() -> set[str]:
    payload = json.loads(_REGISTRY.read_text(encoding="utf-8"))
    return {c["id"] if isinstance(c, dict) else str(c) for c in payload["connectors"]}


def _catalog_ids() -> set[str]:
    return set(
        re.findall(
            r"^- \*\*.*?\(`([a-z0-9-]+)`\)\*\*",
            _CATALOG.read_text(encoding="utf-8"),
            re.M,
        )
    )


def test_the_catalog_lists_every_connector_and_only_those():
    registry, listed = _registry_ids(), _catalog_ids()
    assert not registry - listed, (
        f"connecteurs livres et absents de la page publiee : {sorted(registry - listed)}. "
        "Une page catalogue incomplete se lit comme une page complete."
    )
    assert not listed - registry, (
        f"connecteurs annonces au public et INEXISTANTS : {sorted(listed - registry)}. "
        "C'est la classe du defaut Mailgun : une promesse de donnee qu'aucun module ne livre."
    )


def test_the_announced_count_is_the_real_one():
    text = _CATALOG.read_text(encoding="utf-8")
    match = re.search(r"Connector Catalog \((\d+) Built-in", text)
    assert match, "le titre du catalogue ne porte plus son compte -- il est la garde du reste"
    assert int(match.group(1)) == len(_registry_ids()), (
        f"la page annonce {match.group(1)} connecteurs, le registre en porte {len(_registry_ids())}"
    )


def test_no_published_page_cites_a_module_directory_that_does_not_exist():
    real = {p.name for p in _MODULES.iterdir() if p.is_dir()}
    placeholders = {"<source>", "source", "name", "your-connector"}
    offenders: dict[str, list[str]] = {}
    for page in sorted(_DOCS.glob("*.mdx")):
        cited = set(
            re.findall(
                r"server/modules/([a-z0-9][a-z0-9_-]*)",
                page.read_text(encoding="utf-8"),
            )
        )
        missing = sorted(cited - real - placeholders)
        if missing:
            offenders[page.name] = missing
    assert not offenders, (
        f"chemins de module cites et inexistants : {offenders}. "
        "Un chemin faux dans un tutoriel envoie le lecteur construire dans le vide."
    )


@pytest.mark.parametrize("page", sorted(p.name for p in _DOCS.glob("*.mdx")))
def test_the_retired_product_noun_stays_retired_in_published_copy(page: str):
    if page in _HISTORICAL:
        pytest.skip("note de version datee : elle dit ce qui etait vrai a sa date")
    offending = [
        f"{page}:{n}: {line.strip()}"
        for n, line in enumerate((_DOCS / page).read_text(encoding="utf-8").splitlines(), 1)
        if _RETIRED.search(line) and not any(ok in line for ok in _LEGITIMATE)
    ]
    assert not offending, (
        "`Module` et `Extension` sont retires comme noms de produit "
        "(docs/product-architecture/glossary.md). L'objet est un Connector.\n  "
        + "\n  ".join(offending)
    )


def test_the_mintlify_navigation_and_the_files_agree():
    config = json.loads((_DOCS / "docs.json").read_text(encoding="utf-8"))
    declared: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "pages" and isinstance(value, list):
                    for page in value:
                        declared.append(page) if isinstance(page, str) else walk(page)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(config["navigation"])
    on_disk = {p.stem for p in _DOCS.glob("*.mdx")}
    assert not set(declared) - on_disk, (
        f"pages declarees dans docs.json et absentes du disque : {sorted(set(declared) - on_disk)} "
        "-- la publication Mintlify casse dessus"
    )
    assert not on_disk - set(declared), (
        "pages ecrites et hors navigation, donc jamais publiees : "
        f"{sorted(on_disk - set(declared))}"
    )


def test_the_internal_code_pattern_actually_matches_one() -> None:
    r"""THE PROBE, KEPT -- this guard was green for three years' worth of nothing.

    Its pattern was written with literal BACKSPACE characters where word
    boundaries were meant (`\bAD-\d+\b`), so it could never match anything a
    document contains. It passed on every run from the day it was committed

    The `r` prefix on this docstring is not decoration: without it, `\d` is an
    invalid escape and Python says so on every import -- the same class of
    quiet breakage the probe below exists to catch, in the sentence that
    describes it.
    (bf5a298e, 2026-08-03) while three internal codes sat in the published pages:
    `AD-2` and `AD-7` in `adding-a-connector.mdx`, `AD-1` in
    `mcp-host-integration.mdx`. Repairing the pattern turned it red immediately.

    A guard that cannot be shown to bite is a guard nobody should trust. This is
    the demonstration, and it lives beside the guard rather than in a shell
    someone ran once.
    """
    code = re.compile(_INTERNAL_CODE)
    assert code.findall("provenance is written once (AD-7), never rewritten") == ["AD-7"]
    assert code.findall("see CAP-19 and NFR11 and AI-262") == ["CAP-19", "NFR11", "AI-262"]
    # And the converse: a word that merely CONTAINS the letters is not a code.
    assert code.findall("the ROAD-9 sign and a GRAD-1 label") == []


def test_no_published_page_cites_an_internal_decision_code():
    """`AD-9` ne veut rien dire pour qui lit la doc du produit.

    Mesure du 2026-08-03 : 52 references `AD-<n>` dans 12 pages publiees, dont 24
    pour la seule page destinee aux utilisateurs d'agents, qui s'intitulait
    « Agent Rules & System Invariants (AD-1 to AD-32) ». Ces codes designent des
    decisions ratifiees dans un document interne que le lecteur n'a pas. La
    contrainte qu'ils portent est utile ; leur numero ne l'est pas -- il faut
    l'ecrire, pas le referencer.
    """
    code = re.compile(_INTERNAL_CODE)
    offenders = {
        page.name: sorted({m for m in code.findall(page.read_text(encoding="utf-8"))})
        for page in sorted(_DOCS.glob("*.mdx"))
        if code.search(page.read_text(encoding="utf-8"))
    }
    assert not offenders, (
        f"codes de decision internes dans la doc publiee : {offenders}. "
        "Ecrire la contrainte, pas son numero -- le lecteur n'a pas le registre."
    )


def test_every_command_a_published_page_gives_can_actually_be_run():
    """Une commande inventee envoie le lecteur dans le mur, et je l'ai fait.

    En reecrivant le tutoriel j'ai cite `scripts/scaffold_connector.py`, qui
    n'existe pas -- exactement la faute que la reecriture reparait. Un test
    coute moins cher que la vigilance.
    """
    missing: dict[str, list[str]] = {}
    for page in sorted(_DOCS.glob("*.mdx")):
        cited = set(re.findall(r"scripts/[a-z0-9_]+\.py", page.read_text(encoding="utf-8")))
        absent = sorted(c for c in cited if not (_REPO_ROOT / c).is_file())
        if absent:
            missing[page.name] = absent
    assert not missing, f"scripts cites et inexistants : {missing}"


def _documentation_tab_pages() -> list[str]:
    """The pages of the product Documentation tab, read from the Mintlify config.

    The Self-Hosted tab is deliberately excluded: `adding-a-connector.mdx`
    documents the PYTHON functions a connector author writes (`pull`,
    `transform`, `classify_http_error`) and those are not MCP tools. Splitting on
    the tab is not a convenience -- it is the line between "what an agent can
    call" and "what an engineer implements".
    """
    config = json.loads((_DOCS / "docs.json").read_text(encoding="utf-8"))
    pages: list[str] = []
    for tab in config["navigation"]["tabs"]:
        if tab["tab"] != "Documentation":
            continue
        for group in tab["groups"]:
            pages.extend(p for p in group["pages"] if isinstance(p, str))
    assert pages, "l'onglet Documentation a disparu de docs.json"
    return pages


def _registered_tool_names() -> set[str]:
    from core import main as core_main  # noqa: F401,PLC0415  (assembles the surface)
    from core import mcp_profiles  # noqa: PLC0415

    return {d.name for d in mcp_profiles.registered_declarations()}


def test_the_tool_name_pattern_actually_matches_one() -> None:
    """LA SONDE. Le defaut trouve le 2026-09-04 avait trois formes.

    `### ``get_morning_briefing``` en titre, ``get_morning_briefing(project_id)``
    en prose, et toute une famille ``<source>_report`` citee dans une puce :
    `- ``google_analytics_report(project_id, date_from, date_to)```. Une garde qui
    ne lirait que les titres aurait laisse passer les deux dernieres.
    """
    assert _TOOL_HEADING.findall("### `get_morning_briefing`\n") == ["get_morning_briefing"]
    bullet = "- `google_analytics_report(project_id)`"
    assert _TOOL_CALL.findall(bullet) == ["google_analytics_report"]
    assert _TOOL_CALL.findall("get_daily_report(\n    project_id: str") == ["get_daily_report"]
    # Une methode qualifiee n'est pas un outil : elle n'est pas collee au backtick.
    assert _TOOL_CALL.findall("`core.anomaly_alerts.minimum_observations_for_threshold()`") == []


def test_every_tool_a_published_page_names_exists_on_the_server():
    """Mesure du 2026-09-04, avant ce test :

      - `agent-tools.mdx` se disait « exhaustive reference for all FastMCP
        tools » et en documentait NEUF sur 131 ;
      - trois de ces neuf n'existaient pas : `get_morning_briefing`,
        `connector_activate`, `connector_verify` ;
      - une famille entiere etait annoncee et n'a jamais ete construite : un
        outil `<source>_report` par connecteur, `mailgun_report` compris -- pour
        un transport qui n'est meme pas une source ;
      - `get_morning_briefing` etait aussi cite par deux autres pages.

    C'est la classe du defaut Mailgun, transposee aux outils : une promesse
    d'appel qu'aucun serveur ne peut tenir, et qui se lit exactement comme une
    page a jour.
    """
    registry = _registered_tool_names()
    #: Noms de CHAMP cites comme du code et suivis d'une parenthese nulle part.
    #: Un seul aujourd'hui -- s'il en faut beaucoup, c'est le motif qui derive.
    not_a_tool = {"authored_by"}
    offenders: dict[str, list[str]] = {}
    for page in _documentation_tab_pages():
        text = (_DOCS / f"{page}.mdx").read_text(encoding="utf-8")
        named = set(_TOOL_HEADING.findall(text)) | set(_TOOL_CALL.findall(text))
        unknown = sorted(named - registry - not_a_tool)
        if unknown:
            offenders[page] = unknown
    assert not offenders, (
        f"outils annonces au public et INEXISTANTS : {offenders}. "
        "Un appel invente envoie l'agent dans le mur, et le lecteur ne peut pas le savoir."
    )


def _boundary_policy() -> dict:
    import tomllib

    return tomllib.loads(
        (_REPO_ROOT / "distribution" / "public-app.toml").read_text(encoding="utf-8")
    )


def _boundary_tables(page: str) -> tuple[str, str]:
    """Les deux TABLEAUX, pas les deux moities de la page.

    La section << Export & Release Workflow >> vient apres le tableau prive et
    cite `docs/` legitimement (Mintlify le lit). Decouper la page en deux et
    appeler la seconde moitie << prive >> rendait la prose coupable.
    """
    public = page.split("## A public directory")[0]
    private = page.split("## Private Workspace Projection", 1)[1].split("\n---", 1)[0]
    return public, private

def test_the_published_boundary_page_agrees_with_the_allow_list():
    """`public-app.toml` decide ; cette page l'EXPLIQUE. Elle avait derive.

    Mesure du 2026-09-06. `repository-boundary.mdx` rangeait `docs/` du cote
    PRIVE -- alors que le fichier d'allow-list le publie, et que
    `docs.toorow.com` EST ce dossier, servi par Mintlify depuis le depot public.
    Elle citait aussi `pnpm-workspace.yaml` parmi les fichiers racine publies,
    qui n'y est pas, et omettait six chemins prives. Rien ne pouvait le dire :
    une page de politique perimee se lit exactement comme une page juste, et
    celle-ci decrit la seule operation du depot qui soit irreversible.

    La page n'a pas a etre generee -- elle explique, et une explication vaut
    mieux qu'un tableau derive. Mais chaque nom qu'elle porte doit exister dans
    la source de verite, et chaque nom de la source doit y figurer.
    """
    policy = _boundary_policy()
    page = (_DOCS / "repository-boundary.mdx").read_text(encoding="utf-8")

    missing_public = [d for d in policy["public_directories"] if f"`{d}/`" not in page]
    assert not missing_public, (
        f"repertoires publies et absents de la page : {missing_public}"
    )

    missing_private = [
        p for p in policy["private_paths"] if f"`{p}`" not in page and f"`{p}/`" not in page
    ]
    assert not missing_private, (
        f"chemins prives et absents de la page : {missing_private}"
    )

    #: Ce qui est publie et ce qui ne l'est pas ne peuvent pas etre le meme mot.
    public_table, private_table = _boundary_tables(page)
    wrongly_public = [p for p in policy["private_paths"] if f"`{p}/`" in public_table]
    assert not wrongly_public, (
        f"chemins prives presentes comme publies : {wrongly_public}"
    )
    assert "`web/`" in private_table and "`studio/`" in private_table

    missing_root = [f for f in policy["root_files"] if f"`{f}`" not in page]
    assert not missing_root, f"fichiers racine publies et absents de la page : {missing_root}"

    invented_root = [
        name
        for name in re.findall(r"`([A-Za-z0-9_.-]+\.(?:toml|lock|md|yaml|yml))`", page)
        if name not in policy["root_files"]
        and name not in {"public-app.toml", "docs.json", "manifest.json"}
        and not (_REPO_ROOT / name).exists()
    ]
    assert not invented_root, (
        f"fichiers racine annonces publies et INEXISTANTS : {invented_root}. "
        "C'est ainsi que `pnpm-workspace.yaml` a survecu a sa suppression."
    )

    docs_exclusions = [g for g in policy["exclude_globs"] if g.startswith("docs/")]
    assert docs_exclusions, "plus aucune exclusion sous docs/ -- la page en decrit encore"
    unnamed = [g for g in docs_exclusions if g.replace("/**", "") not in page]
    assert not unnamed, (
        f"exclusions de docs/ absentes de la page : {unnamed}. "
        "Un repertoire publie n'ouvre pas tout son sous-arbre, et la page doit dire lequel."
    )


def test_the_boundary_guard_would_catch_the_drift_it_was_written_for():
    """LA SONDE. Le defaut etait `docs/` du mauvais cote d'un tableau.

    Une garde qui ne verifie que la PRESENCE d'un nom ne l'aurait pas vu : le
    mot `docs/` etait bien sur la page -- rangee dans la mauvaise moitie.
    """
    policy = _boundary_policy()
    assert "docs" in policy["public_directories"]
    assert "docs" not in policy["private_paths"]

    page = (_DOCS / "repository-boundary.mdx").read_text(encoding="utf-8")
    assert "## Private Workspace Projection" in page, "la coupe des deux tableaux a disparu"
    _, private_table = _boundary_tables(page)
    assert "`docs/`" not in private_table, (
        "`docs/` est reapparu du cote prive -- c'est exactement la derive de 2026-09-06"
    )
