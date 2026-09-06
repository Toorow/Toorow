"""Deux gardes, deux frontieres PRECISES : AD-2 (le core ignore toute source)
et AD-1 (la narration ne LIT aucune donnee).

Une seule implementation par garde (`scripts/check_core_source_agnostic.py`,
`scripts/check_narrative_no_raw.py`), deux portes : celle-ci pour le run local,
`make check-core-source-agnostic` / `make check-narrative-no-raw` pour la CI.

CE QUE CES TESTS TIENNENT, ET CE N EST PAS LA LISTE. Les gardes precedents
etaient des greps de chaines, et chacun rougissait a tort en permanence : AD-2
confondait une VALEUR de donnee (`verification_source_type == "ga4"`) avec une
logique source ; AD-1 interdisait la CITATION `(connector:fact_daily_kpi,
pull_id)` que AD-9 exige. Un garde rouge en permanence n en est plus un : on
apprend a l ignorer.

La ligne a ne pas franchir, dans les deux sens : un garde qui refuse le
legitime se desactive (les tests "reste vert"), un garde qui ne connait que le
legitime ne sert a rien (les tests "rougit sur").
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CORE_SCRIPT = ROOT / "scripts" / "check_core_source_agnostic.py"
NARRATIVE_SCRIPT = ROOT / "scripts" / "check_narrative_no_raw.py"


def _module(script: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _core_guard():
    return _module(CORE_SCRIPT, "check_core_source_agnostic")


def _narrative_guard():
    return _module(NARRATIVE_SCRIPT, "check_narrative_no_raw")


def _write_core(tmp_path: Path, files: dict[str, str]) -> Path:
    core = tmp_path / "core"
    core.mkdir()
    for name, content in files.items():
        (core / name).write_text(content, encoding="utf-8")
    return core


# ---------------------------------------------------------------------------
# AD-2 -- le core est source-agnostic
# ---------------------------------------------------------------------------


def test_core_is_source_agnostic_on_the_tree():
    assert _core_guard().offences() == []


def test_ad2_guard_turns_red_on_a_module_import(tmp_path):
    core = _write_core(tmp_path, {"evil.py": "from modules.meta_ads import connector\n"})
    offences = _core_guard().offences(core_dir=core)
    assert any("importe le code d un module" in o for o in offences)


def test_ad2_guard_turns_red_on_a_slug_branch(tmp_path):
    core = _write_core(tmp_path, {"evil.py": 'if connector == "meta-ads":\n    pass\n'})
    offences = _core_guard().offences(core_dir=core)
    assert any("branchement sur le slug" in o for o in offences)


def test_ad2_guard_turns_red_on_an_unadjudicated_dynamic_loader(tmp_path):
    core = _write_core(
        tmp_path, {"evil.py": "spec = importlib.util.spec_from_file_location(n, p)\n"}
    )
    offences = _core_guard().offences(core_dir=core)
    assert any("chargement dynamique" in o for o in offences)


def test_ad2_guard_turns_red_on_an_unadjudicated_import_module(tmp_path):
    """import_module est la meme espece de chargement dynamique (C30)."""
    core = _write_core(
        tmp_path, {"evil.py": 'mod = importlib.import_module("inbound.adapters.x")\n'}
    )
    offences = _core_guard().offences(core_dir=core)
    assert any("import_module" in o for o in offences)


def test_ad2_guard_stays_green_on_the_adjudicated_import_module_seams(tmp_path):
    """inbound_seam.py et governance_rule_sets.py : adjuges, avec leur raison."""
    core = _write_core(
        tmp_path,
        {
            "inbound_seam.py": 'return importlib.import_module(_CAPABILITY_INDEX)\n',
            "governance_rule_sets.py": "        importlib.import_module(name)\n",
        },
    )
    assert _core_guard().offences(core_dir=core) == []


def test_ad2_guard_stays_green_on_a_warehouse_mode_comparison(tmp_path):
    """`bigquery` est un module ET un mode d entrepot ; le cote gauche tranche."""
    core = _write_core(
        tmp_path,
        {"warehouse.py": 'if mode == "bigquery":\n    pass\nif dialect != "bigquery":\n    pass\n'},
    )
    assert _core_guard().offences(core_dir=core) == []


def test_ad2_guard_stays_green_on_the_adjudicated_seams(tmp_path):
    """Registre et taxonomie de verification : adjuges, jamais ajoutes en silence."""
    core = _write_core(
        tmp_path,
        {
            "datastream_preconfiguration_api.py": 'if loaded.name == "google-sheets":\n    pass\n',
            "cards.py": 'if verification_source_type == "shopify":\n    pass\n',
            "loader.py": "spec = importlib.util.spec_from_file_location(n, p)\n",
        },
    )
    assert _core_guard().offences(core_dir=core) == []


def test_ad2_slugs_are_derived_from_the_modules_directory(tmp_path):
    """Un 41e module entre dans le garde sans que personne y pense."""
    modules = tmp_path / "modules"
    (modules / "acme-ads").mkdir(parents=True)
    assert "acme-ads" in _core_guard().module_slugs(modules)


# ---------------------------------------------------------------------------
# AD-1 -- la narration ne LIT aucune donnee
# ---------------------------------------------------------------------------


def test_narrative_reads_no_data_on_the_tree():
    assert _narrative_guard().offences() == []


def _narrative_file(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "narrative.py"
    path.write_text(content, encoding="utf-8")
    return path


def test_ad1_guard_turns_red_on_a_data_access_import(tmp_path):
    path = _narrative_file(tmp_path, "import psycopg\nfrom core import warehouse\n")
    offences = _narrative_guard().offences(path)
    assert len([o for o in offences if "acces aux donnees" in o]) == 2


def test_ad1_guard_turns_red_on_sql(tmp_path):
    path = _narrative_file(tmp_path, 'query = "SELECT * FROM t"\n')
    assert any("SQL" in o for o in _narrative_guard().offences(path))


def test_ad1_guard_turns_red_on_a_raw_relation_reference(tmp_path):
    path = _narrative_file(tmp_path, 'name = "raw_ga4_standard_daily"\n')
    assert any("relation brute" in o for o in _narrative_guard().offences(path))


def test_ad1_guard_stays_green_on_the_citation_token(tmp_path):
    """LA LIGNE A NE PAS FRANCHIR : nommer la relation lue est la preuve,
    pas la lecture. Le garde precedent interdisait le geste qu il protegeait."""
    path = _narrative_file(
        tmp_path,
        'token = f"({source_system}:fact_daily_kpi, {pull_id})"\n',
    )
    assert _narrative_guard().offences(path) == []
