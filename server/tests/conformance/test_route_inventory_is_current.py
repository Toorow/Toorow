"""`screens/routes.json` says what the server serves -- or it says nothing.

WHY THIS FILE EXISTS. `scripts/screens.py:781` reads the inventory as a CACHE:

    if ROUTES_JSON.exists():
        return set(json.loads(read(ROUTES_JSON)))

so it is written once and never checked again. Everything downstream believes
it: `screens.py` decides from it whether a page calls an endpoint that exists,
and the API plane of `screens/wiring.md` is drawn from that verdict. Nothing in
`server/tests` read the file at all -- `grep -rln routes.json server/tests`
returned nothing -- so a route that moved left the inventory quietly wrong and
every reader downstream quietly wrong with it.

Measured when this guard was written (2026-08-07): the file was **416** paths
against a router of **426**, missing the day-grain read of story 58.1, the
progress route of 63.2, the stop route of 63.6, the bounded sample and the
export -- and still naming `/api/datastreams/{}/refetch`, which story 58.4 had
removed. Ten surfaces' worth of drift, none of it noticed by anything.

IT COMPARES, IT DOES NOT REGENERATE. A guard that rewrote the file would be a
guard that can never fail. When it goes red, run the one command in the message.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ROUTES_JSON = ROOT / "screens" / "routes.json"

#: The normalisation `scripts/screens.py:793` applies -- `{project_id}` and
#: `{id}` are the same hole to a reader asking "does this endpoint exist".
_HOLE = re.compile(r"\{[^}]*\}")


def _router_paths() -> set[str]:
    from core.admin_api import router

    return {_HOLE.sub("{}", getattr(route, "path", "")) for route in router.routes}


def _inventory() -> set[str]:
    return set(json.loads(ROUTES_JSON.read_text(encoding="utf-8")))


def test_the_inventory_names_exactly_the_paths_the_router_mounts() -> None:
    mounted, inventory = _router_paths(), _inventory()

    missing = sorted(mounted - inventory)
    phantom = sorted(inventory - mounted)
    assert not missing and not phantom, (
        "`screens/routes.json` no longer describes this server.\n"
        f"  mounted and NOT in the inventory ({len(missing)}): {missing}\n"
        f"  in the inventory and NOT mounted ({len(phantom)}): {phantom}\n\n"
        "Regenerate it from the router itself:\n"
        "  cd server && python -c \"import json,re,sys; sys.path.insert(0,'.'); "
        "from core.admin_api import router; "
        "print(json.dumps(sorted({re.sub(r'[{][^}]*[}]','{}',r.path) "
        "for r in router.routes}), indent=1))\" > ../screens/routes.json\n\n"
        "A phantom path is the worse half: `screens.py` reads this file to decide "
        "whether a screen calls an endpoint that exists, so a route that moved "
        "leaves every page calling it looking wired to something that answers 404."
    )


def test_the_guard_is_not_vacuous() -> None:
    """Both sides really carry the paths -- an empty comparison passes anything."""
    mounted, inventory = _router_paths(), _inventory()

    assert len(mounted) > 300, len(mounted)
    assert len(inventory) > 300, len(inventory)
    # And the shape is normalised on both sides, or the comparison would be an
    # accident: a raw `{project_id}` on one side matches nothing on the other.
    assert all("{" not in path or "{}" in path for path in inventory)
