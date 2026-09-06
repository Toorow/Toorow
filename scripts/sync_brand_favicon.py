"""Mirror the toorow browser icon from ONE source onto every published surface.

WHY THIS EXISTS. Measured 2026-09-04: `docs/favicon.ico` and
`docs/public/favicon.ico` (docs.toorow.com) carried a mark that is not the
toorow mark. The cloud had been stretched to fill a square canvas, so its two
holes rendered as vertical ovals and the flat cloud base was cropped away.
`web/public/favicon.ico` (toorow.com) carried the right shape. Both were
hand-placed binaries, so nothing could tell them apart -- a wrong icon and a
right icon are the same kind of file. Only a derivation from the canonical set
can separate them.

THE SOURCE. `logo/toorow_icon_<n>x<n>.png` -- the brand icon rendered once, at
each size a browser asks for, with its aspect ratio intact. This script does no
resampling: it packages those exact bytes into an ICO container, so the icon a
tab shows is the icon the brand set holds, byte for byte.

THE SURFACES. Every file a browser is pointed at as `rel="icon"`:
  * `docs/favicon.ico` + `docs/public/favicon.ico` -- Mintlify (`docs.json`
    declares `/favicon.ico`; Mintlify serves it from both roots).
  * `web/public/favicon.ico` -- the Astro marketing site.
The admin console points at `/brand/toorow-icon-512.png`, already a byte copy of
`logo/toorow_icon_512x512.png`; this script checks that copy and never rewrites
a PNG into an ICO.

`--check` reports drift and returns 1 (the gate, mirroring
`sync_connector_logos.py --check`). `--write` applies.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAND_DIR = ROOT / "logo"

#: The sizes a browser actually requests for a tab, a bookmark and a taskbar.
ICO_SIZES = (16, 24, 32, 48)

#: Every file served to a browser as `rel="icon"`, as an ICO.
ICO_SURFACES = (
    ROOT / "docs" / "favicon.ico",
    ROOT / "docs" / "public" / "favicon.ico",
    ROOT / "web" / "public" / "favicon.ico",
)

#: Surfaces that point at a raw PNG instead: (served file, canonical source).
PNG_SURFACES = (
    (
        ROOT / "ui" / "admin" / "public" / "brand" / "toorow-icon-512.png",
        BRAND_DIR / "toorow_icon_512x512.png",
    ),
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _read_png(size: int) -> bytes:
    """The canonical mark at one size -- verified to BE a PNG of that size.

    A checksum would only say "these bytes did not change"; it blesses a JPEG,
    an ICO or an HTML error page saved under a .png name just as happily. The
    dimensions are read out of the IHDR chunk, which is the only thing that
    proves the brand mark was not squashed on its way in.
    """
    path = BRAND_DIR / f"toorow_icon_{size}x{size}.png"
    blob = path.read_bytes()
    if blob[:8] != PNG_MAGIC:
        raise SystemExit(f"{path} n'est pas un PNG -- la source de la marque est corrompue")
    width, height = struct.unpack(">II", blob[16:24])
    if (width, height) != (size, size):
        raise SystemExit(
            f"{path} mesure {width}x{height} et devrait mesurer {size}x{size} -- "
            "un icone hors carre est la marque deformee que ce script repare"
        )
    return blob


def build_ico() -> bytes:
    """Package the canonical PNGs into one ICO. No resampling happens here."""
    payloads = [_read_png(size) for size in ICO_SIZES]
    header = struct.pack("<HHH", 0, 1, len(payloads))
    offset = len(header) + 16 * len(payloads)
    directory, body = b"", b""
    for size, blob in zip(ICO_SIZES, payloads):
        directory += struct.pack(
            "<BBBBHHII", size, size, 0, 0, 1, 32, len(blob), offset
        )
        body += blob
        offset += len(blob)
    return header + directory + body


def _targets() -> list[tuple[Path, bytes]]:
    ico = build_ico()
    targets: list[tuple[Path, bytes]] = [(path, ico) for path in ICO_SURFACES]
    targets += [(served, source.read_bytes()) for served, source in PNG_SURFACES]
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="report drift, write nothing")
    mode.add_argument("--write", action="store_true", help="mirror the canonical icon")
    args = parser.parse_args(argv)

    drifted: list[Path] = []
    for path, expected in _targets():
        current = path.read_bytes() if path.is_file() else None
        if current == expected:
            continue
        drifted.append(path)
        if args.write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(expected)

    relative = [str(p.relative_to(ROOT)).replace("\\", "/") for p in drifted]
    if args.write:
        print(f"synced {len(drifted)} file(s): {', '.join(relative) or '(already in lockstep)'}")
        return 0
    if drifted:
        print(
            "l'icone servie n'est pas l'icone de la marque : "
            + ", ".join(relative)
            + "\nrelancer : python scripts/sync_brand_favicon.py --write"
        )
        return 1
    print("brand icon in lockstep on every surface")
    return 0


if __name__ == "__main__":
    sys.exit(main())
