"""Conformance — l'icone servie par chaque surface EST l'icone de la marque.

Mesure du 2026-09-04 : `docs/favicon.ico` (docs.toorow.com) portait un nuage
etire sur un canevas carre — deux ovales verticaux a la place des deux cercles,
la base plate du nuage rognee. `web/public/favicon.ico` portait la bonne forme.
Les deux etaient des binaires poses a la main : rien ne pouvait les distinguer,
une icone fausse et une icone juste sont le meme genre de fichier.

Ce test compare chaque fichier servi a la DERIVATION du jeu canonique
(`logo/toorow_icon_<n>x<n>.png`), qui est la seule chose qu'un lecteur ne peut
pas confondre avec une copie perimee.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import sync_brand_favicon as brand  # noqa: E402


def test_every_served_icon_is_the_canonical_mark():
    drifted = [
        str(path.relative_to(_REPO_ROOT)).replace("\\", "/")
        for path, expected in brand._targets()
        if not path.is_file() or path.read_bytes() != expected
    ]
    assert not drifted, (
        f"icone servie != icone de la marque : {drifted}. "
        "Relancer `python scripts/sync_brand_favicon.py --write`."
    )


def test_the_derivation_keeps_the_mark_square_and_unstretched():
    """La sonde : le defaut repare etait un ETIREMENT, pas un octet de travers.

    Une garde qui ne compare que des octets passerait encore si la source elle
    meme etait ecrasee. `_read_png` lit les dimensions dans le chunk IHDR et
    refuse tout ce qui n'est pas le carre annonce — c'est cette lecture, et
    elle seule, qui empeche la marque de revenir deformee.
    """
    for size in brand.ICO_SIZES:
        blob = brand._read_png(size)
        assert blob[:8] == brand.PNG_MAGIC

    ico = brand.build_ico()
    assert ico[:6] == b"\x00\x00\x01\x00" + bytes([len(brand.ICO_SIZES), 0])
    entries = {ico[6 + 16 * i] or 256 for i in range(len(brand.ICO_SIZES))}
    assert entries == set(brand.ICO_SIZES)


def test_the_mintlify_config_points_at_a_file_this_guard_covers():
    """Une garde qui couvre un fichier que personne ne sert ne garde rien."""
    import json

    config = json.loads((_REPO_ROOT / "docs" / "docs.json").read_text(encoding="utf-8"))
    declared = config["favicon"].lstrip("/")
    covered = {path.name for path, _ in brand._targets()}
    assert declared in covered, (
        f"docs.json sert /{declared}, hors du perimetre de ce test : {sorted(covered)}"
    )
