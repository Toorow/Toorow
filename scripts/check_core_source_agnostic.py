#!/usr/bin/env python3
r"""Le core ne sait rien d aucune source. Le garde le verifiait sur des CHAINES.

CE QU IL ETAIT (ci.yml, job guards, jusqu au 2026-08-20) :

    grep -rE "google-analytics|meta-ads|ga4|gsc|google_analytics" server/core/

Trois faiblesses, et la premiere suffisait a le rendre inutilisable :

  1. IL CONFONDAIT UNE VALEUR AVEC UNE LOGIQUE. `verification_source_type ==
     "ga4"` (narrative.py, projects_api.py) compare une TAXONOMIE DE DONNEES --
     trois valeurs gouvernees (`ga4`, `shopify`, `stripe`) qui circulent dans
     les lignes, pas une connaissance de l API Google. `connector_scope:
     "google-analytics"` (cards.py) est une portee lue, pas un branchement.
     Le grep rougissait sur chacune -- et sur les commentaires, les docstrings,
     et meme un fichier binaire oublie (`scratch_test.duckdb`).
  2. IL NE CONNAISSAIT QUE CINQ NOMS, en dur. Un 40e module pouvait etre
     nomme dans le core sans que rien ne rougisse.
  3. IL NE POUVAIT PAS PASSER, donc le job `guards` etait rouge en
     permanence -- et un garde rouge en permanence n en est plus un : on
     apprend a l ignorer.

CE QU IL EST MAINTENANT. Trois regles qui visent la DEPENDANCE, pas la mention :

  A. Le core n IMPORTE jamais le code d un module (`from/import modules...`).
  B. Le chargement dynamique est un seam ADJUGE, pas une porte ouverte. Deux
     formes, deux listes :
     `importlib.util.spec_from_file_location` (_ADJUDICATED_LOADERS) --
     `loader.py` (le registre) et `raw_table_provisioning.py` (la recolte du
     DDL DECLARE), chacun generique (aucun nom de module dans leur code) ;
     `importlib.import_module` (_ADJUDICATED_IMPORTERS) -- `inbound_seam.py`
     (LA racine de composition AD-2 : resout des CAPABILITIES, jamais un
     provider) et `governance_rule_sets.py` (registre de profils CORE, liste
     explicite et greppable). Tout nouvel endroit rougit et doit etre adjuge
     a son tour.
  C. Le core ne BRANCHE jamais sur le slug d un module : aucune comparaison
     `==` / `!=` contre un nom de dossier de `server/modules/`, la liste etant
     DERIVEE du dossier -- un 41e module entre dans le garde sans que
     personne y pense.

CE QU IL NE REFUSE PAS, ET C EST LE POINT DELICAT.
Trois formes sont des comparaisons legitimes qu un grep ne distingue pas :

  * les modes WAREHOUSE (`mode == "bigquery"`, `dialect`, `backend`, `target`)
    -- `bigquery` est a la fois un module ET un mode d entrepot ; le cote
    gauche de la comparaison dit lequel des deux on regarde ;
  * le seam du REGISTRE : `loaded.name == "google-sheets"`
    (datastream_preconfiguration_api.py) demande au registre ce que le
    deploiement porte, au lieu d importer -- c est la reponse AD-2, pas sa
    violation ;
  * la taxonomie de VERIFICATION : `verification_source_type == "shopify"`
    (cards.py) lit un enum de donnees gouverne, pas une API.

Les deux dernieres sont ADJUGEES ci-dessous, fichier par fichier, avec leur
raison -- une nouvelle occurrence rougit et doit etre adjugee a son tour,
jamais ajoutee en silence.

    python scripts/check_core_source_agnostic.py           # rapport
    python scripts/check_core_source_agnostic.py --gate    # non-zero si offence

Le meme code est appele par
`server/tests/conformance/test_core_purity_guards.py` : une regle qui vit dans
deux codes finit par vivre dans deux verites.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "server" / "core"
MODULES = ROOT / "server" / "modules"

#: Comparaisons slug ADJUGEES legitimes, (nom de fichier, slug) -> raison.
#: Toute nouvelle occurrence doit etre adjugee ici avec SA raison, ou supprimee.
_ADJUDICATED: dict[tuple[str, str], str] = {
    (
        "datastream_preconfiguration_api.py",
        "google-sheets",
    ): "seam du registre: le module registry repond ce que le deploiement porte, "
    "au lieu d importer le connecteur (la reponse AD-2, pas sa violation)",
    (
        "cards.py",
        "shopify",
    ): "taxonomie de verification: enum de donnees gouverne "
    "{ga4, shopify, stripe} (projects_api._VERIFICATION_SOURCE_TYPES), "
    "pas une connaissance de l API Shopify",
    (
        "warehouse.py",
        "bigquery",
    ): "taxonomie de MOTEUR d entrepot: `engine` vaut {duckdb, bigquery} "
    "(TOOROW_DB_MODE), le dialecte ou la requete s execute -- pas le module "
    "source bigquery de server/modules/",
}

#: Regle A -- importer le code d un module. Le `modules` vise est le paquet
#: `server/modules` ; `import modules` nu ou `from server.modules...` pareil.
_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(?:server\.)?modules(?:\.|\s+import\b)")

#: Regle B -- le chargement dynamique est un seam ADJUGE, fichier -> raison.
#: Tout nouveau fichier chargeant dynamiquement doit etre adjuge ici, avec la
#: preuve que son parcours des modules reste GENERIQUE (aucun slug dans son code).
_ADJUDICATED_LOADERS: dict[str, str] = {
    "loader.py": "le registre: decouverte et import de tout connecteur valide, "
    "sans jamais en nommer un",
    "raw_table_provisioning.py": "la recolte du DDL DECLARE par chaque "
    "connector.py (f-string deja resolue), aucun nom de module dans le fichier",
}
_SPEC_LOAD_RE = re.compile(r"spec_from_file_location")

#: Regle B bis -- `importlib.import_module` est la meme espece de chargement
#: dynamique, avec sa propre liste d seams adjuges (meme philosophie que
#: _ADJUDICATED_LOADERS : chaque fichier y entre avec SA raison, jamais en
#: silence).
_ADJUDICATED_IMPORTERS: dict[str, str] = {
    "inbound_seam.py": "la racine de composition AD-2: resout des CAPABILITIES "
    "inbound par import_module (cible unique 'inbound.capabilities'), sans "
    "jamais nommer un provider",
    "governance_rule_sets.py": "le registre de profils: importe des modules "
    "CORE (_PROFILE_MODULES, liste explicite et greppable), aucun module source",
}
_IMPORT_MODULE_RE = re.compile(r"\bimport_module\s*\(")

#: Regle C -- le cote gauche qui dit "mode d entrepot", jamais un connecteur.
_WAREHOUSE_MODE_LHS = re.compile(r"(?:^|[._])(?:db_)?(?:mode|dialect|backend|target)$")


def module_slugs(modules_dir: Path = MODULES) -> list[str]:
    """Les slugs sont LUS du dossier des modules, jamais recopies ici."""
    return sorted(
        p.name
        for p in modules_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".") and not p.name.startswith("_")
    )


def _comparison_re(slugs: list[str]) -> re.Pattern[str]:
    alternation = "|".join(re.escape(s) for s in slugs)
    return re.compile(
        rf"(?P<lhs>[A-Za-z_][\w.]*)\s*(?:==|!=)\s*"
        rf"(?P<q>['\"])(?P<slug>{alternation})(?P=q)"
    )


def offences(core_dir: Path = CORE, modules_dir: Path = MODULES) -> list[str]:
    """Toutes les offences AD-2 du core, une chaine par offence (vide = propre)."""
    found: list[str] = []
    slugs = module_slugs(modules_dir)
    comparison = _comparison_re(slugs)
    for path in sorted(core_dir.rglob("*.py")):
        rel = path.relative_to(core_dir).as_posix()
        name = path.name
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _IMPORT_RE.search(line):
                found.append(
                    f"{rel}:{lineno}: le core importe le code d un module -- "
                    f"le dispatch passe par le registre (loader), "
                    f"jamais par un import: {line.strip()}"
                )
            if name not in _ADJUDICATED_LOADERS and _SPEC_LOAD_RE.search(line):
                found.append(
                    f"{rel}:{lineno}: chargement dynamique hors des seams adjuges "
                    f"({', '.join(sorted(_ADJUDICATED_LOADERS))}) -- un nouveau seam "
                    f"se justifie dans scripts/check_core_source_agnostic.py, avec la "
                    f"preuve qu il reste generique: {line.strip()}"
                )
            if name not in _ADJUDICATED_IMPORTERS and _IMPORT_MODULE_RE.search(line):
                found.append(
                    f"{rel}:{lineno}: import_module hors des seams adjuges "
                    f"({', '.join(sorted(_ADJUDICATED_IMPORTERS))}) -- un nouveau seam "
                    f"se justifie dans scripts/check_core_source_agnostic.py, avec la "
                    f"preuve qu il reste generique: {line.strip()}"
                )
            if line.lstrip().startswith("#"):
                continue
            for match in comparison.finditer(line):
                lhs, slug_value = match.group("lhs"), match.group("slug")
                if _WAREHOUSE_MODE_LHS.search(lhs):
                    continue
                if (name, slug_value) in _ADJUDICATED:
                    continue
                found.append(
                    f"{rel}:{lineno}: branchement sur le slug d un module "
                    f"({slug_value!r}) -- la connaissance source vit dans "
                    f"server/modules/, pas dans le core (AD-2). Si cette "
                    f"comparaison est une taxonomie de donnees ou un seam du "
                    f"registre, adjugez-la dans scripts/check_core_source_agnostic.py "
                    f"avec sa raison: {line.strip()}"
                )
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--gate", action="store_true", help="exit non-zero si une offence")
    args = parser.parse_args()
    found = offences()
    if found:
        print("AD-2 OFFENCES (le core doit ignorer toute source specifique):")
        for offence in found:
            print(f"  {offence}")
        return 1 if args.gate else 0
    print(
        f"OK: server/core/ n importe aucun module, ne charge dynamiquement que "
        f"dans les seams adjuges ({', '.join(sorted(_ADJUDICATED_LOADERS))}; "
        f"import_module: {', '.join(sorted(_ADJUDICATED_IMPORTERS))}), "
        f"et ne branche sur aucun des {len(module_slugs())} slugs."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
