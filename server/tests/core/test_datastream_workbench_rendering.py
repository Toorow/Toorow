"""Le rendu est une etape du contrat, pas un detail d'emballage.

Toutes les suites du Workbench asserten le DICTIONNAIRE que `read_tab` renvoie.
Aucune ne le RENDAIT. `starlette.responses.JSONResponse` appelle `json.dumps`
sans `default` : un `datetime` -- la forme ordinaire de toute colonne
`timestamptz` -- levait `TypeError` APRES que le handler ait reussi, et le
fourre-tout du seam le traduisait en 503 « Datastream evidence is unavailable ».

Mesure du 2026-08-04 sur la base reelle : **3 onglets sur 6** repondaient 503
en permanence -- `overview`, `mapping`, `processing`. L'onglet `overview` porte
le panneau d'adresse de livraison entrante, donc l'adresse `ds_<token>@...`
etait invisible pour cette seule raison.

Ce test rend chaque onglet avec des horodatages presents. Il echoue si un
onglet redevient non serialisable.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from core.datastream_workbench import TABS
from core.json_encoding import SafeJSONResponse, json_default


def _payload_with_timestamps(tab: str) -> dict:
    """La forme minimale qu'un onglet renvoie: des horodatages natifs, imbriques."""
    moment = datetime(2026, 8, 4, 21, 51, 36, tzinfo=UTC)
    return {
        "schema": f"datastream_workbench.{tab}.v1",
        "tab": tab,
        "evidence": {
            "schedule": {"next_run_at": moment, "updated_at": moment},
            "latest_execution": {"state_changed_at": moment},
            "stage_coverage": [{"stage": "load", "latest_at": moment}],
            "published_at": moment,
        },
    }


@pytest.mark.parametrize("tab", TABS)
def test_every_tab_renders_with_native_timestamps(tab: str) -> None:
    body = SafeJSONResponse(_payload_with_timestamps(tab)).render(
        _payload_with_timestamps(tab)
    )
    assert b"2026-08-04T21:51:36Z" in body, "un horodatage doit sortir en ISO 8601 UTC"


def test_a_datetime_alone_would_break_the_stock_response() -> None:
    """La garde nommee: sans `default`, le rendu echoue -- c'etait le defaut."""
    import json

    with pytest.raises(TypeError):
        json.dumps({"next_run_at": datetime(2026, 8, 4, tzinfo=UTC)})


def test_binary_is_never_serialized_to_a_client() -> None:
    with pytest.raises(TypeError):
        json_default(b"secret-bytes")
