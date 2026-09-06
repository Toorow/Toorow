"""Revision-bound, server-owned discovery evidence for Datastream setup."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable

from core.audit import declare_action

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12). Celles-ci n'etaient declarees NULLE PART : la valeur
# etait retapee en dur ici, parce que la liste centrale de `core/audit.py`
# etait trop loin pour valoir le detour. Mesure ce jour-la sur le journal
# vivant : 29 des 64 actions reellement ecrites -- 45 % -- etaient dans ce
# cas, et rien ne pouvait distinguer une action d'une faute de frappe.
ACTION_DATASTREAM_SETUP_OBSERVATION_CREATED = declare_action("datastream.setup_observation.created")


logger = logging.getLogger(__name__)

MODES = {"connector_pull", "external_bq", "managed_feed"}
DISCOVERY_KINDS = {
    "connector_pull": {"connector_contract", "connector_fields"},
    "external_bq": {"warehouse_schema"},
    "managed_feed": {"file_schema", "sheet_schema", "channel_contract"},
}
CHANNELS = {"file_upload", "google_sheets", "inbound_email", "webhook"}
COMMON_KEYS = {"expected_revision", "mode", "discovery_kind"}
MODE_KEYS = {
    "connector_pull": {
        "source_account_ref",
        "connector_ref",
        "connector_contract_version_ref",
        "report_ref",
    },
    "external_bq": {
        "access_ref",
        "object_ref",
        "declared_writer",
        "readonly_acknowledged",
    },
    "managed_feed": {
        "channel",
        "source_account_ref",
        "template_ref",
        "staged_asset_ref",
        "sheet_ref",
    },
}
FORBIDDEN_KEYS = {
    "credential",
    "credentials",
    "secret",
    "access_token",
    "refresh_token",
    "provider_account_id",
    "external_account_id",
    "file_bytes",
    "raw_payload",
    "raw_sample",
    "sample_rows",
}
SAFE_METADATA_KEYS = {
    "fields",
    "field_ids",
    # LES OBJETS ADRESSABLES QUE LA DECOUVERTE A VUS -- les onglets d'un
    # classeur (57.2), et les objets d'un canal (57.3) sous la MEME forme.
    # Ecrite une fois pour les trois adaptateurs : la question « lequel de ces
    # objets voulez-vous ? » est la meme, et un ecran qui la fait retaper au
    # clavier demande un nom que la decouverte connait deja.
    "objects",
    "schema_hash",
    "location",
    "watermark",
    "freshness",
    "row_count_bucket",
    "detected_format",
    "content_hash",
    "staged_asset_ref",
    "report_refs",
    "date_fields",
    "grain",
    "history",
    "cadence",
    "quota_cost",
    "append_supported",
    "logical_name",
}


class ObservationValidationError(ValueError):
    code = "invalid_observation_request"


class ObservationConflict(RuntimeError):
    code = "observation_revision_conflict"


class ObservationNotFound(LookupError):
    code = "not_found"


class ObservationUnavailable(RuntimeError):
    code = "observation_unavailable"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key).lower()
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def validate_discovery_request(value: dict[str, Any]) -> dict[str, Any]:
    """Validate the browser's bounded discriminated discovery request."""
    if not isinstance(value, dict):
        raise ObservationValidationError("Discovery request must be an object")
    forbidden = sorted(set(_walk_keys(value)) & FORBIDDEN_KEYS)
    if forbidden:
        raise ObservationValidationError(
            f"Discovery request contains forbidden keys: {', '.join(forbidden)}"
        )
    revision = value.get("expected_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ObservationValidationError("expected_revision must be a positive integer")
    mode = value.get("mode")
    if mode not in MODES:
        raise ObservationValidationError("mode is unsupported")
    kind = value.get("discovery_kind")
    if kind not in DISCOVERY_KINDS[mode]:
        raise ObservationValidationError("discovery_kind is not valid for this mode")
    allowed = COMMON_KEYS | MODE_KEYS[mode]
    irrelevant = sorted(set(value) - allowed)
    if irrelevant:
        raise ObservationValidationError(
            f"Mode-irrelevant discovery fields are not accepted: {', '.join(irrelevant)}"
        )
    # `connector_contract_version_ref` IS NOT REQUIRED, and that is the binding
    # rule: a contract is PINNED when a Connector is bound, not before. Demanding
    # the ref here made the browser quote a row that only existed if somebody had
    # pre-written one per module -- the reason 39 rows were fabricated. Sent, it
    # is honoured and checked; absent, `_validate_reference_scope` pins the
    # module's declared contract at that moment and returns the ref it minted.
    required = {
        "connector_pull": {"source_account_ref", "connector_ref"},
        "external_bq": {"access_ref", "object_ref", "declared_writer", "readonly_acknowledged"},
        "managed_feed": {"channel"},
    }[mode]
    missing = sorted(key for key in required if value.get(key) in (None, "", False))
    if missing:
        raise ObservationValidationError(f"Missing required discovery fields: {', '.join(missing)}")
    if mode == "external_bq":
        coordinates = str(value.get("object_ref") or "").split(".")
        if len(coordinates) != 3 or any(not part.strip() for part in coordinates):
            raise ObservationValidationError(
                "BigQuery object_ref must be project.dataset.table_or_view"
            )
    if mode == "managed_feed" and value.get("channel") not in CHANNELS:
        raise ObservationValidationError("Managed-feed channel is unsupported")
    if len(_canonical(value).encode("utf-8")) > 4096:
        raise ObservationValidationError("Discovery request exceeds the bounded input limit")
    return deepcopy(value)


def _safe_field(field: Any) -> dict[str, Any] | None:
    if not isinstance(field, dict):
        return None
    # `description` is the schema's OWN sentence about a column -- BigQuery's
    # column description, a connector contract's field description. It is
    # metadata written by whoever declared the column, never a row value, and it
    # is what step 2 shows instead of the example value this projection refuses
    # (`raw_sample` / `sample_rows` are in FORBIDDEN_KEYS, and they stay there).
    result = {
        key: field[key]
        for key in ("name", "field_id", "type", "kind", "nullable", "mode", "description")
        if key in field and isinstance(field[key], (str, bool, int, float, type(None)))
    }
    return result or None


def _safe_object(item: Any) -> dict[str, Any] | None:
    """Un objet adressable : ce qu'on peut choisir, et ce qu'une personne lit.

    DEUX CLES, PAS UNE DE PLUS. `object_ref` est la valeur que l'écran écrira
    dans la référence de source ; `label` est ce qu'il affiche. Une chaîne nue
    est acceptée et vaut les deux : pour un onglet de tableur, le titre EST
    l'adresse. Aucune autre clé ne passe -- un objet n'est pas un endroit où
    faire voyager autre chose (`FORBIDDEN_KEYS` s'applique en amont, et ce
    filtre est le second).
    """
    if isinstance(item, str):
        text = item.strip()
        return {"object_ref": text, "label": text} if text else None
    if not isinstance(item, dict):
        return None
    object_ref = str(item.get("object_ref") or "").strip()
    if not object_ref:
        return None
    label = str(item.get("label") or object_ref).strip()
    return {"object_ref": object_ref[:200], "label": (label or object_ref)[:200]}


# Les types physiques qui sont un DOSSIER, pas une colonne.
CONTAINER_FIELD_TYPES = {"record", "struct", "object", "array", "repeated"}


def is_container_field(field: Any) -> bool:
    """Ce champ décrit-il un groupe de colonnes plutôt qu'une colonne ?

    UN CHAMP DECRIT N'EST PAS UN CHAMP OFFRABLE, et c'est la distinction que la
    story 57.1 avait tranchée du mauvais côté : pour empêcher qu'un `STRUCT`
    devienne une dimension sélectionnable, le client typé ne l'émettait pas du
    tout -- donc un opérateur dont la moitié des colonnes sont imbriquées voyait
    une table amputée sans que rien ne le dise.

    La règle vit ici, une fois : la découverte DECRIT tout ce qu'elle lit, et le
    compilateur refuse d'en faire une colonne. Un `RECORD` est un dossier ; un
    champ `REPEATED` est un tableau qu'aucun `UNNEST` ne déplie dans ce produit.
    """
    if not isinstance(field, dict):
        return False
    physical = str(field.get("physical_type") or field.get("type") or "").strip().lower()
    mode = str(field.get("mode") or "").strip().lower()
    return physical in CONTAINER_FIELD_TYPES or mode == "repeated"


def _safe_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 3 or isinstance(value, (bytes, bytearray)):
        return None
    if isinstance(value, (str, bool, int, float, type(None))):
        return value
    if isinstance(value, list) and len(value) <= 200:
        return [_safe_json(item, depth=depth + 1) for item in value]
    if (
        isinstance(value, dict)
        and len(value) <= 100
        and not (set(_walk_keys(value)) & FORBIDDEN_KEYS)
    ):
        return {str(key): _safe_json(child, depth=depth + 1) for key, child in value.items()}
    return None


# La taille maximale de `safe_metadata`, et le nombre maximal de champs listés.
SAFE_METADATA_BUDGET_BYTES = 8192
MAX_OBSERVED_FIELDS = 200
# Le nombre maximal d'objets adressables listés. Même borne que les champs, et
# elle se DIT (`coverage.object_list`) : une liste bornée qui se tait se lit
# comme entière, et l'opérateur conclut que son onglet n'existe pas.
MAX_OBSERVED_OBJECTS = 200

# LES CLES QUI NE SE SACRIFIENT JAMAIS.
#
# Chacune répond à une question qu'aucune autre ne répond -- ce que la lecture
# coûterait, quelle empreinte de schéma a été vue, où vit l'objet, quels champs
# existent -- et toutes tiennent en quelques dizaines d'octets face aux 8192.
# `quota_cost` est la plus critique depuis la story 57.1 : la preview d'un
# entrepôt externe la REFUSE quand elle manque, donc la perdre au rognage
# fabriquait un refus dont la réparation affichée (« relancez la découverte »)
# ne pouvait jamais aboutir -- le second passage perdait la même clé.
ESSENTIAL_METADATA_KEYS = ("quota_cost", "schema_hash", "location", "field_ids")

# Les clés qui sont des LISTES, donc les seules qu'on puisse raccourcir plutôt
# que jeter. Une liste raccourcie garde son sens ; une clé jetée n'en a plus.
# `objects` y entre avec elle-même (57.2) : sans cette ligne, un classeur à
# soixante onglets aurait fait jeter la clé ENTIERE, et l'écran aurait dit « ce
# classeur n'a aucun onglet » -- le défaut exact que la reprise de 57.1 a
# réparé pour `fields`.
_TRIMMABLE_LIST_KEYS = ("fields", "field_ids", "objects")


def _fit_safe_metadata(safe: dict[str, Any]) -> None:
    """Ramener `safe_metadata` sous son budget SANS jeter ce qui décide.

    LE ROGNEUR PRECEDENT EJECTAIT DES CLES ENTIERES, par ordre alphabétique
    inverse. Mesuré le 2026-08-05 : à 60 champs portant une description de 40
    caractères, il ne restait que 6 clés sur 18 -- ni `fields`, ni `quota_cost`.
    Trois conséquences, toutes silencieuses :

    * un objet de 60 colonnes s'affichait comme « aucune colonne lisible », un
      vide FABRIQUE, ce que la ligne « Vide et cassé » du contrat interdit ;
    * la borne de 200 champs que les adaptateurs annoncent était fausse : tout
      sautait au-delà d'une cinquantaine ;
    * depuis la story 57.1, `quota_cost` ouvre un refus 422 sur la preview, donc
      une table large devenait une impasse sans réparation possible.

    L'ordre est maintenant : raccourcir la plus longue liste, puis -- seulement
    si la place manque encore -- jeter les clés non essentielles. Les essentielles
    ne partent pas ; celle qui peut grandir (`field_ids`) se raccourcit.
    """

    def size() -> int:
        return len(_canonical(safe).encode("utf-8"))

    while size() > SAFE_METADATA_BUDGET_BYTES:
        candidates = [
            key
            for key in _TRIMMABLE_LIST_KEYS
            if isinstance(safe.get(key), list) and safe[key]
        ]
        if not candidates:
            break
        longest = max(candidates, key=lambda key: len(safe[key]))
        items = safe[longest]
        per_item = max(1, len(_canonical(items).encode("utf-8")) // len(items))
        over = size() - SAFE_METADATA_BUDGET_BYTES
        drop = max(1, min(len(items), over // per_item + 1))
        safe[longest] = items[: len(items) - drop]

    while size() > SAFE_METADATA_BUDGET_BYTES:
        droppable = [key for key in reversed(list(safe)) if key not in ESSENTIAL_METADATA_KEYS]
        if not droppable:
            break
        safe.pop(droppable[0])


def normalize_adapter_evidence(value: dict[str, Any]) -> dict[str, Any]:
    """Deny-by-default projection of untrusted adapter output."""
    if not isinstance(value, dict):
        raise ObservationValidationError("Adapter evidence must be an object")
    raw = value.get("safe_metadata")
    raw = raw if isinstance(raw, dict) else {}
    # Compté AVANT toute borne : c'est le seul moment où le nombre de champs
    # réellement lus est connu, et c'est ce nombre que l'écran doit pouvoir
    # comparer à ce qu'il affiche.
    observed_fields = len(raw["fields"]) if isinstance(raw.get("fields"), list) else None
    observed_objects = len(raw["objects"]) if isinstance(raw.get("objects"), list) else None
    safe: dict[str, Any] = {}
    for key in sorted(SAFE_METADATA_KEYS):
        child = raw.get(key)
        if key == "fields" and isinstance(child, list):
            safe[key] = [
                field for item in child[:MAX_OBSERVED_FIELDS] if (field := _safe_field(item))
            ]
        elif key == "objects" and isinstance(child, list):
            # Bornée ICI plutôt que par le passage générique en dessous, qui
            # jette une liste dépassant 2048 octets ENTIEREMENT : une liste
            # d'onglets un peu longue serait devenue « aucun onglet », ce que le
            # contrat d'écran appelle un vide fabriqué.
            safe[key] = [
                entry for item in child[:MAX_OBSERVED_OBJECTS] if (entry := _safe_object(item))
            ]
        elif isinstance(child, (str, bool, int, float, type(None))):
            safe[key] = child
        elif isinstance(child, (list, dict)) and len(_canonical(child).encode("utf-8")) <= 2048:
            projected = _safe_json(child)
            if projected is not None:
                safe[key] = projected
    _fit_safe_metadata(safe)
    coverage = value.get("coverage") if isinstance(value.get("coverage"), dict) else {}
    exceptions = value.get("exceptions") if isinstance(value.get("exceptions"), list) else []
    normalized = {
        "adapter_ref": str(value.get("adapter_ref") or "unavailable"),
        "safe_metadata": safe,
        "coverage": {
            str(key): str(state)
            for key, state in sorted(coverage.items())[:50]
            if isinstance(key, str) and isinstance(state, (str, bool, int, float))
        },
        "exceptions": [
            {"code": str(item.get("code"))}
            for item in exceptions[:50]
            if isinstance(item, dict) and item.get("code")
        ],
    }
    # UNE LISTE RACCOURCIE LE DIT. Même règle que la liste d'objets bornée de la
    # story 57.1 : une liste qui se tait sur sa borne se lit comme entière, et
    # l'opérateur conclut que ses colonnes n'existent pas.
    if observed_fields is not None:
        listed = len(normalized["safe_metadata"].get("fields") or [])
        normalized["coverage"]["fields_observed"] = str(observed_fields)
        normalized["coverage"]["fields_listed"] = str(listed)
        normalized["coverage"]["field_list"] = (
            "complete" if listed == observed_fields else "truncated"
        )
        if listed < observed_fields:
            normalized["exceptions"].append({"code": "field_list_truncated"})
    # MEME REGLE POUR LA LISTE D'OBJETS, et pour la même raison : une liste
    # d'onglets bornée qui se tait se lit comme le classeur entier, et
    # l'opérateur conclut que le sien n'existe pas -- alors qu'il faut lire
    # « nommez-le vous-même ». C'est la clause de la story 57.1 sur les objets
    # BigQuery, tenue ici par le normalisateur pour les trois adaptateurs.
    if observed_objects is not None:
        listed_objects = len(normalized["safe_metadata"].get("objects") or [])
        normalized["coverage"]["objects_observed"] = str(observed_objects)
        normalized["coverage"]["objects_listed"] = str(listed_objects)
        normalized["coverage"]["object_list"] = (
            "complete" if listed_objects == observed_objects else "truncated"
        )
        if listed_objects < observed_objects:
            normalized["exceptions"].append({"code": "object_list_truncated"})
    normalized["evidence_fingerprint"] = _hash(normalized)
    return normalized


Adapter = Callable[[dict[str, Any]], dict[str, Any]]

# UN SEUL mecanisme de decouverte : l'adaptateur est INJECTE par l'appelant
# (`create_observation(adapter=...)`), ou bien la paire (mode, discovery_kind)
# est declaree non couverte ci-dessous. Il n'y a pas de troisieme porte.
#
# AI-122 -- pourquoi le registre a disparu. Ce module portait un registre public
# `register_setup_adapter` / `_ADAPTERS` / `observe_with_registered_adapter` que
# PERSONNE n'alimentait : mesure du 2026-08-04,
#     grep -rn "register_setup_adapter" server --include="*.py"
# ne rendait que sa propre definition, et aucun fichier de test ne le nommait.
# Pendant ce temps `datastream_preconfiguration_api.py` construisait ses
# adaptateurs en closure et les passait a la main. Deux mecanismes pour la meme
# chose, dont le plus visible etait le mort : le prochain qui ajoutait un
# adaptateur lisait le registre -- la porte qui avait l'air prevue pour lui --
# et son adaptateur n'aurait jamais ete appele.
#
# L'arbitrage « enregistrer les closures OU supprimer le registre » se tranche
# des qu'on regarde ce que SONT ces closures : elles capturent la connexion de
# la requete et ses `path_params`. Les poser dans un dict global de processus y
# ferait survivre une connexion apres la fin de la requete, et surtout la
# requete SUIVANTE -- celle d'un autre projet -- reutiliserait une closure liee
# au `project_id` / `draft_id` de la precedente. C'est mot pour mot la classe
# AI-125 (rattachement inter-tenant silencieux). Donc le registre disparait, et
# l'injection reste le seul chemin.

# Les paires qui ont un adaptateur reellement cable, et par qui.
WIRED_ADAPTERS: dict[tuple[str, str], str] = {
    # `create_observation` ci-dessous, branche `mode == "connector_pull"`, qui
    # demande la capacite `observe_connector_contract` au seam AD-2. Le module
    # qui la sert n'est PAS nomme ici : c'est exactement ce que le seam achete.
    ("connector_pull", "connector_contract"): "capability:observe_connector_contract",
    ("connector_pull", "connector_fields"): "capability:observe_connector_contract",
    # Closures injectees par `datastream_preconfiguration_api._attach_observation`.
    # Deux branches selon le canal, meme paire : `inbound_email`/`webhook` va a
    # `core.inbound_discovery.observe_first_delivery`, tout le reste (dont
    # `file_upload`) va a `observe_staged_file`.
    ("managed_feed", "file_schema"): "datastream_preconfiguration_api (closure)",
    # Story 57.1. Closure injectee par `_create_observation`, sur le client type
    # `modules.bigquery.connector.BigQuerySetupReader` -- qui REUTILISE
    # `describe_table` / `_flatten_schema` et le dry-run du connecteur plutot que
    # d'ouvrir un second acces a BigQuery.
    ("external_bq", "warehouse_schema"): "datastream_preconfiguration_api (closure)",
    # Story 57.2. Meme forme : closure injectee par `_create_observation`, sur le
    # client type `modules.google-sheets.connector.SheetsSetupReader` -- qui
    # REUTILISE `_fetch_sheet_values` et la taxonomie d'erreurs du connecteur, et
    # n'ajoute que l'appel qui manquait (`spreadsheets.get`, sans grille).
    ("managed_feed", "sheet_schema"): "datastream_preconfiguration_api (closure)",
    # Story 57.3. Meme forme, et une difference qui compte : cette paire n'a
    # AUCUN fournisseur a interroger. Un e-mail et un webhook ne se listent pas,
    # ils PROMETTENT -- la closure projette donc une declaration de l'operateur
    # relue contre l'etat du deploiement (`core.inbound_discovery.
    # read_channel_contract`), et n'appelle rien.
    #
    # `inbound_email` garde `file_schema` : l'e-mail est un TRANSPORT du fichier,
    # et le fichier arrive EST son observation. `channel_contract` est la paire du
    # `webhook`, qui n'a aucun fichier tant que rien n'est arrive.
    ("managed_feed", "channel_contract"): "datastream_preconfiguration_api (closure)",
}

# Les paires SANS adaptateur, nommees plutot que subies. Elles rendent
# `adapter_unavailable`, ce qui compile la section `fields` avec le blocker
# `missing_connector_contract_or_observation` (datastream_preconfiguration.py).
#
# IL N'EN RESTE AUCUNE depuis la story 57.3 (2026-08-05). `sheet_schema` avait
# demenage avec 57.2, `channel_contract` ferme la table. La table reste, et
# `uncovered_adapter_evidence` avec elle : elle est le repli de
# `create_observation` pour une paire qu'une story future ajouterait a
# `DISCOVERY_KINDS` sans la brancher -- un trou muet redeviendrait alors nomme,
# ce qui est exactement ce que cette table achete.
UNCOVERED_ADAPTERS: dict[tuple[str, str], str] = {}


def uncovered_adapter_evidence(mode: str, discovery_kind: str) -> dict[str, Any]:
    """Evidence normalisee pour une paire qu'aucun adaptateur ne couvre.

    Dit LAQUELLE : `adapter_ref` porte la paire, ce qui distingue « personne n'a
    encore branche external_bq » d'une panne d'adaptateur.
    """
    return normalize_adapter_evidence(
        {
            "adapter_ref": f"{mode}.{discovery_kind}.unavailable",
            "coverage": {"discovery": "unavailable"},
            "exceptions": [{"code": "adapter_unavailable"}],
        }
    )


def observed_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return deepcopy(default)
    return json.loads(value) if isinstance(value, str) else deepcopy(value)


def _observation_payload(row: tuple[Any, ...], *, replay: bool = False) -> dict[str, Any]:
    return {
        "observation_ref": row[0],
        "project_ref": row[1],
        "draft_ref": row[2],
        "draft_revision_ref": row[3],
        "draft_revision": int(row[4]),
        "mode": row[5],
        "discovery_kind": row[6],
        "adapter_ref": row[7],
        "connector_contract_version_ref": row[8],
        "request_fingerprint": row[9],
        "evidence_fingerprint": row[10],
        "schema_hash": row[11],
        "safe_metadata": _json(row[12], {}),
        "coverage": _json(row[13], {}),
        "exceptions": _json(row[14], []),
        "observed_at": row[15].isoformat() if hasattr(row[15], "isoformat") else str(row[15]),
        "expires_at": row[16].isoformat() if row[16] and hasattr(row[16], "isoformat") else row[16],
        "idempotent_replay": replay,
    }


_OBSERVATION_SELECT = """
SELECT id,project_id,draft_id,draft_revision_id,draft_revision,mode,discovery_kind,
       adapter_ref,connector_contract_version_ref,request_fingerprint,
       evidence_fingerprint,schema_hash,safe_metadata,coverage,exceptions,
       observed_at,expires_at
FROM app.datastream_setup_observations
WHERE id=%s AND draft_id=%s AND project_id=%s
"""


def _onboarding_modes(manifest: Any) -> list[str]:
    """The wizard modes this module can be reached through, from its manifest.

    THE MODE HAS TO NARROW WHAT FOLLOWS. Every one of the 39 manifests declares
    `public_catalog.onboarding_modes`, and the option projection dropped it -- so
    `Connector pull` offered all 39 cards, `google-sheets` and `generic` among
    them, which are `managed_feed` channels and cannot be pulled. Choosing a mode
    and being offered the products of another mode is the step contradicting its
    own first question.

    A manifest that declares nothing is offered on the pull path, which is what
    the catalogue did for every module before this projection existed.
    """
    value = _json(manifest, {})
    contract = value.get("contract") if isinstance(value.get("contract"), dict) else value
    catalogue = contract.get("public_catalog")
    modes = catalogue.get("onboarding_modes") if isinstance(catalogue, dict) else None
    if not isinstance(modes, list):
        return ["connector_pull"]
    return [str(mode) for mode in modes if isinstance(mode, str)] or ["connector_pull"]


def _connector_source_category(connector_ref: str) -> dict[str, Any]:
    """The connector's `public_catalog.category`, plus where it was read from.

    ONE source, and it is the module manifest -- the same field the fee/tax ladder
    derives from (`fee_tax_source_types.fetch_derivation_signals`). The wizard shows
    it read-only: a category describes the PRODUCT, not the Datastream, so a second
    authority on it would diverge from the manifest at the next catalogue change.

    Tolerant, exactly like the ladder's own read: an unreadable or category-less
    manifest yields `None`, never an exception. Step 1 must still render when one
    connector out of thirty-nine has a broken manifest, and the field says the
    category is absent rather than pretending one.
    """
    from core.context_seed import load_registry_entry  # noqa: PLC0415

    try:
        entry = load_registry_entry(connector_ref)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "datastream_setup_observations: registry entry unreadable connector=%s: %s",
            connector_ref,
            exc,
        )
        return {"source_category": None, "source_category_origin": None}
    manifest = (entry or {}).get("manifest") or {}
    public_catalog = manifest.get("public_catalog") or {}
    category = public_catalog.get("category")
    if not isinstance(category, str) or not category:
        return {"source_category": None, "source_category_origin": None}
    # A proposal shows its evidence source or it is not a proposal
    # (datastream-workbench-and-wizard.md, `Prefill and proposal rules`).
    return {"source_category": category, "source_category_origin": "connector_manifest"}


# THE WINDOW AND THE OFFSET BELONG TO THE PLATFORM, NOT TO THE CONNECTOR.
#
# Measured on the 2026-08-05: none of the 133 `report_profiles` entries carries
# `window` or `offset`. Both values are column defaults --
# `app.datastreams.date_window_days` (`023_datastreams.sql:74`) and
# `app.datastreams.window_offset_days` (`206_datastream_window_offset.sql:12`),
# re-read at creation in `datastreams.py:248-249`.
#
# They travel WITH THEIR ORIGIN so the screen can label them `Platform default`.
# Rendered bare they would read as something the Connector declared, which is
# "no screen fabricates a value" applied to a value that is true but wrongly
# attributed -- the harder half of that rule.
PLATFORM_WINDOW_DEFAULTS = {
    "date_window_days": 30,
    "window_offset_days": 1,
    "origin": "platform_default",
}


def deployment_environment() -> str:
    """The environment a contract row is scoped to. One reading, one default."""
    return os.environ.get("TOOROW_ENVIRONMENT", "production").strip() or "production"


def registry_connector_catalogue(conn) -> list[dict[str, Any]]:
    """The Connector catalogue: the REGISTRY, read from the modules on disk.

    THE CATALOGUE IS NOT PINNED TO AN INSTANCE. It used to be the rows of
    `app.connector_contract_versions`, so a Connector existed for the operator
    only once somebody had written a row for it -- which is how 39 rows came to
    be written into production for modules nobody had bound, data that answered
    a read question by pre-answering it. And it was briefly narrowed to what a
    Project's authorizations already open, which hides from a person the exact
    Connector they came here to add. Neither is a catalogue. The catalogue is
    every module the registry holds; adding a module adds an entry, and nothing
    else has to happen for it to be offerable.

    WHAT THE PERSISTED ROW IS FOR, then, is the PIN: the contract a binding
    committed to, recorded at the moment of the binding. It gives each entry its
    `contract_state`, and that is the whole of its job here:

      `unverified` -- nothing has bound this Connector yet, so nothing is pinned.
      `verified`   -- a pin exists and the module still declares that contract.
      `stale`      -- a pin exists and the module has CHANGED under it. Read from
                      the fingerprints, so a manifest edit is visible the next
                      time the step is opened rather than silently rewriting what
                      an existing Datastream was built on.

    The reports and fields always come from the MANIFEST, never from the pinned
    snapshot: the catalogue's job is to say what the module offers today.

    Tolerant, one module at a time: an unreadable manifest drops its own entry
    and is logged, because one broken module out of thirty-nine must not take
    the step down.
    """
    from core.context_seed import load_registry_entry, registry_module_names  # noqa: PLC0415
    from core.data_identities import connector_contract_fingerprint  # noqa: PLC0415

    environment = deployment_environment()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (connector_id) connector_id,id,connector_fingerprint,created_at "
            "FROM app.connector_contract_versions WHERE environment=%s "
            "ORDER BY connector_id,version_number DESC",
            (environment,),
        )
        pinned = {str(row[0]): row for row in cur.fetchall()}

    catalogue: list[dict[str, Any]] = []
    for connector_name in registry_module_names():
        try:
            entry = load_registry_entry(connector_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "datastream_setup_observations: manifest unreadable connector=%s: %s",
                connector_name,
                exc,
            )
            continue
        manifest = (entry or {}).get("manifest")
        # A Connector with no declared capabilities has no report for step 2 to
        # offer, so listing it would be a dead end reached AFTER the selection.
        if not isinstance(manifest, dict) or not manifest.get("source_capabilities"):
            continue
        declared_fingerprint = connector_contract_fingerprint(manifest)
        pin = pinned.get(connector_name)
        if pin is None:
            state, version_ref, observed_at = "unverified", "", None
        elif str(pin[2]) == declared_fingerprint:
            state, version_ref = "verified", str(pin[1])
            observed_at = pin[3].isoformat() if hasattr(pin[3], "isoformat") else str(pin[3])
        else:
            # A STALE PIN IS NOT AN OFFER. It used to travel exactly like a
            # verified one, and naming a version ref is what makes the binding
            # ADOPT it instead of pinning what the module declares now
            # (`_validate_reference_scope`). So the step offered today's bundle,
            # the binding adopted yesterday's, and the compile refused the
            # operator's selection with `exact_bundle_required` -- a refusal no
            # gesture on the screen could repair, because the screen was already
            # showing the only bundle it offers. Measured 2026-08-12 in
            # production. Empty, as for a Connector never bound: the binding
            # pins what is declared now, which is what the person is looking at.
            state, version_ref = "stale", ""
            observed_at = pin[3].isoformat() if hasattr(pin[3], "isoformat") else str(pin[3])
        catalogue.append(
            {
                "connector_ref": connector_name,
                "manifest": manifest,
                "contract_version_ref": version_ref,
                # THE MODULE'S fingerprint, not the pin's. This is what the entry
                # is describing; the pin's own fingerprint is what `stale` was
                # decided from and does not need to travel twice.
                "contract_fingerprint": declared_fingerprint,
                "contract_state": state,
                "observed_at": observed_at,
            }
        )
    return catalogue


# Les identifiants de Connecteur qui adressent un entrepot BigQuery externe. Une
# seule liste, lue par la projection des acces ET par la verification de scope.
BIGQUERY_CONNECTORS = ("bigquery", "google-bigquery")


def _bigquery_listing_bound() -> int:
    """Combien d'objets la decouverte BigQuery liste au plus par dataset.

    Lu SUR LE CONNECTEUR (`MAX_DISCOVERY_TABLES_PER_DATASET`) via le registre de
    modules, jamais reecrit ici : un second litteral divergerait le jour ou la
    marche change, et l'ecran affirmerait une completude que personne n'a
    mesuree. Le registre est interroge par `core.main.get_loaded_modules`, donc
    core n'importe pas `server/modules/*` (AD-2).

    Borne introuvable -> `0`, ce qui marque TOUS les datasets comme tronques :
    la direction sure est de rouvrir la saisie libre, jamais de promettre que la
    liste est complete.
    """
    try:
        from core.main import get_loaded_modules  # noqa: PLC0415

        for loaded in get_loaded_modules():
            if loaded.name in BIGQUERY_CONNECTORS:
                bound = getattr(loaded.connector_module, "MAX_DISCOVERY_TABLES_PER_DATASET", None)
                if isinstance(bound, int) and not isinstance(bound, bool) and bound > 0:
                    return bound
    except Exception as exc:  # noqa: BLE001 -- une borne inconnue n'est pas une panne
        logger.warning("datastream_setup_observations: BigQuery listing bound unreadable: %s", exc)
    return 0


def _three_part_object_ref(value: Any) -> str | None:
    """Le nom de scope quand il EST un `project.dataset.table`, sinon `None`."""
    text = str(value or "").strip()
    parts = text.split(".")
    if len(parts) != 3 or any(not part.strip() for part in parts):
        return None
    return text


def _external_access_options(
    source_accounts: list[dict[str, Any]], source_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Les acces BigQuery exposes, chacun disant s'il nomme un objet et s'il ment.

    Pour ce Connecteur une FEUILLE est une table (`connector.py`, seul le noeud
    `table` porte un `id`), donc le nom de scope d'un Source Account est deja la
    reference a trois parties que l'etape 1 demandait de retaper. Elle est rendue
    ici pour que le champ devienne une lecture.

    ET LA LISTE DIT QU'ELLE EST BORNEE. La decouverte s'arrete a
    `MAX_DISCOVERY_TABLES_PER_DATASET` objets par dataset et pose `truncated` sur
    le noeud du dataset -- un drapeau qu'aucune colonne ne conserve, puisque seule
    la feuille devient une ligne de `credential_accounts`. Il est reconstruit par
    comptage : un dataset dont le nombre d'objets exposes atteint la borne a ete
    coupe, et le champ doit se rouvrir en saisie libre plutot que de cacher des
    centaines d'objets derriere une lecture qui a l'air complete.
    """
    bound = _bigquery_listing_bound()
    # LE MEME PREDICAT QUE LE CANAL GOOGLE SHEETS, et pour la meme raison : un
    # Source Account est un scope d'AUTORISATION, donc `connector_ref.id` peut
    # etre le provider et ne nommer aucun outil. Filtrer ici sur cette seule
    # colonne rendait cette liste vide pour toute autorisation dont le provider
    # ne s'ecrit pas comme le Connecteur (story 57.2, defaut de classe).
    scoped = [
        (account, row)
        for account, row in zip(source_accounts, source_rows, strict=True)
        if account_serves_connector(account, BIGQUERY_CONNECTORS)
    ]
    listed_per_dataset: dict[str, int] = {}
    for _account, row in scoped:
        object_ref = _three_part_object_ref(row.get("external_account_id"))
        if object_ref:
            dataset = object_ref.rsplit(".", 1)[0]
            listed_per_dataset[dataset] = listed_per_dataset.get(dataset, 0) + 1
    options: list[dict[str, Any]] = []
    for account, row in scoped:
        object_ref = _three_part_object_ref(row.get("external_account_id"))
        listed = listed_per_dataset.get(object_ref.rsplit(".", 1)[0], 0) if object_ref else 0
        options.append(
            {
                **account,
                "external_object_ref": object_ref,
                "listed_objects": listed,
                "listing_bound": bound,
                # Deux raisons distinctes de laisser la saisie ouverte, et l'ecran
                # les dit separement : l'acces ne nomme pas d'objet a trois
                # parties, ou son dataset a touche la borne.
                "truncated": object_ref is None or bound <= 0 or listed >= bound,
            }
        )
    return options


def _report_profiles_by_id(snapshot: Any) -> dict[str, dict[str, Any]]:
    """The manifest's `report_profiles`, keyed by report id.

    THE NAME OF A REPORT NEVER TRAVELLED, and it is measurable: `display_name`
    exists on 2 of the 133 entries of `source_capabilities.reports`, and on
    133 of 133 `report_profiles`. Reading it only on the capability catalog
    therefore showed 131 cards carrying a technical identifier, which is not a
    card. `source_capabilities.py:832` already joins the two for its own route;
    this is the same join, made where the wizard reads.

    Both blocks are in the persisted snapshot (`data_identities.py:11-22`
    `_CONTRACT_KEYS`), so the join costs one dictionary already in memory.
    """
    value = _json(snapshot, {})
    manifest = value.get("contract") if isinstance(value.get("contract"), dict) else value
    if not isinstance(manifest, dict):
        return {}
    return {
        str(profile["id"]): profile
        for profile in manifest.get("report_profiles") or []
        if isinstance(profile, dict) and profile.get("id")
    }


def _report_safety(capabilities: dict[str, Any]) -> dict[str, dict[str, str]]:
    """The safety verdict of story 36.8, per report -- NEVER a second opinion.

    `recommend_first_report` is the one authority on what a safe first pull is
    (`datastream-workbench-and-wizard.md:241-247`). It answers for the whole
    catalog: `recommended` names the single safe report, `needs_choice` carries
    the safe candidates, `no_safe_recommendation` names none. This function only
    projects that one answer onto each report id; no criterion is re-decided
    here, and a report the engine did not retain is reported as such rather than
    given a verdict of our own.

    `conn=None` / `datastream_id=None` is what the compiler already does
    (`datastream_preconfiguration.py:505-512`): the engine queries the database
    only to resolve the window of an EXISTING Datastream, and there is none at
    creation.
    """
    from core.first_report_draft import recommend_first_report  # noqa: PLC0415

    ids = [
        str(report["id"])
        for report in capabilities.get("reports") or []
        if isinstance(report, dict) and report.get("id")
    ]
    try:
        verdict = recommend_first_report(
            None,
            project_id="",
            capabilities=capabilities,
            account={"eligible": True},
            actor="datastream-setup-wizard",
            datastream_id=None,
        )
    except Exception as exc:  # noqa: BLE001
        # A NAMED ABSENCE, never a default `recommended`. A card that claimed
        # safety because the engine crashed would be the exact fabrication the
        # engine exists to prevent.
        logger.warning("datastream_setup_observations: safety engine unavailable: %s", exc)
        return {
            report_id: {
                "outcome": "no_safe_recommendation",
                "reason": "safety_engine_unavailable",
            }
            for report_id in ids
        }
    safe_candidates = {
        str(candidate.get("report_id"))
        for candidate in verdict.candidates or []
        if candidate.get("report_id")
    }
    if verdict.outcome == "recommended" and verdict.report_id:
        safe_candidates.add(str(verdict.report_id))
    declined = str((verdict.compatibility or {}).get("reason") or "not_a_safe_candidate")
    safety: dict[str, dict[str, str]] = {}
    for report_id in ids:
        if report_id not in safe_candidates:
            safety[report_id] = {"outcome": "no_safe_recommendation", "reason": declined}
        elif verdict.outcome == "recommended":
            safety[report_id] = {"outcome": "recommended", "reason": "only_safe_report_family"}
        else:
            safety[report_id] = {
                "outcome": "needs_choice",
                "reason": "several_safe_report_families",
            }
    return safety


def _connector_option_contract(
    snapshot: Any, *, contract_state: str = "verified"
) -> dict[str, Any]:
    value = _json(snapshot, {})
    manifest = value.get("contract") if isinstance(value.get("contract"), dict) else value
    capabilities = manifest.get("source_capabilities")
    if not isinstance(capabilities, dict):
        return {"reports": [], "fields": [], "contract_state": contract_state}
    from core.first_report_draft import _safe_grain  # noqa: PLC0415

    profiles = _report_profiles_by_id(snapshot)
    safety = _report_safety(capabilities)
    fields = [
        {
            key: field[key]
            for key in ("field_id", "kind", "description", "physical_type")
            if key in field and isinstance(field[key], str)
        }
        for field in list(capabilities.get("fields") or [])[:500]
        if isinstance(field, dict) and field.get("field_id")
    ]
    reports = [
        {
            "report_ref": str(report["id"]),
            "display_name": str(
                report.get("display_name")
                or (profiles.get(str(report["id"])) or {}).get("display_name")
                or report["id"]
            ),
            "availability": _safe_json(report.get("availability")),
            "metrics": [str(item) for item in list(report.get("metrics") or [])[:500]],
            "dimensions": [str(item) for item in list(report.get("dimensions") or [])[:500]],
            "supported_grains": _safe_json(report.get("supported_grains") or []),
            # The SMALLEST DECLARED grain, by the engine's own picker. `None`
            # when the report declares none: a grain is never constructed.
            "smallest_declared_grain": _safe_grain(report),
            "safety": safety.get(
                str(report["id"]),
                {"outcome": "no_safe_recommendation", "reason": "not_a_safe_candidate"},
            ),
            "history": _safe_json(report.get("history")),
            "cadence": _safe_json(report.get("cadence")),
            "quota_cost": _safe_json(report.get("quota_cost")),
        }
        for report in list(capabilities.get("reports") or [])[:200]
        if isinstance(report, dict) and report.get("id")
    ]
    return {"reports": reports, "fields": fields, "contract_state": contract_state}


# ---------------------------------------------------------------------------
# Recommended starting points -- the `Recommended` the target requires of step 1.
# ---------------------------------------------------------------------------
#
# `datastream-workbench-and-wizard.md:48`, mode `connector_pull`, states the step
# must offer "**Recommended:** best account and report family from project intent
# and observed metadata". Nothing rendered it: the three selects opened empty and
# unranked, which is the blank page an operator meets on `Add Datastream`.
#
# THE SAFETY IS NOT REDECIDED HERE. `first_report_draft.recommend_first_report`
# (story 36.8) already derives what a safe first pull looks like -- selectable
# report, smallest DECLARED grain, metrics/dimensions that are a real subset,
# bounded recent interval, quota estimate -- and returns `needs_choice` with its
# candidates rather than picking silently. This function only pairs it with the
# eligible accounts and orders the result; a second engine would be a second
# opinion on what is safe.
#
# Its own route cannot serve this step: `first-report/recommend` is addressed
# `/datastreams/{datastream_id}/...` and a wizard at step 1 has no Datastream.
# The engine itself needs none -- `datastream_id=None` skips the only query it
# would run -- so it is called directly rather than through that route.
#
# WHAT A CARD DOES NOT DO: it fills the source (account + connector + report
# family) and nothing else. Metrics, grain, date field and window belong to step
# 2, whose own `Recommended` is compiled by `datastream_preconfiguration`. The
# derived grain and cost travel on the card as EVIDENCE to read before clicking,
# never as a silent write into `configure`.

_MAX_RECOMMENDATIONS = 6
_MAX_ACCOUNTS_PER_CONNECTOR = 2


def _raw_source_capabilities(snapshot: Any) -> dict[str, Any]:
    """The governed capability catalog as PERSISTED, keys untouched.

    `_connector_option_contract` renames `id` to `report_ref` for the console.
    The recommendation engine reads `id`; handing it that projection would make
    every report vanish silently instead of failing.
    """
    value = _json(snapshot, {})
    manifest = value.get("contract") if isinstance(value.get("contract"), dict) else value
    if not isinstance(manifest, dict):
        return {}
    capabilities = manifest.get("source_capabilities")
    return capabilities if isinstance(capabilities, dict) else {}


def _account_rank(account: dict[str, Any]) -> tuple[int, int, str]:
    """Order accounts by observed evidence, deterministically and never by guess.

    A healthy authorization first, then one already used by a Datastream (it has
    proven it works), then a stable label so two equal accounts never swap order
    between two reads of the same page.
    """
    states = account.get("states") or {}
    evidence = account.get("evidence") or {}
    healthy = 0 if str(states.get("authorization")) == "healthy" else 1
    unused = 0 if int(evidence.get("used_by_count") or 0) > 0 else 1
    return (healthy, unused, str(account.get("label") or ""))


def _recommendation_card(
    *,
    account: dict[str, Any],
    connector_id: str,
    connector_display_name: str,
    contract_version_ref: str,
    contract_fingerprint: str,
    observed_at: str | None,
    candidate: dict[str, Any],
    reports_by_id: dict[str, dict[str, Any]],
    profiles_by_id: dict[str, dict[str, Any]],
    confidence: str,
    rationale: str,
) -> dict[str, Any]:
    report_id = str(candidate.get("report_id") or "")
    report = reports_by_id.get(report_id) or {}
    profile = profiles_by_id.get(report_id) or {}
    account_id = str((account.get("object_ref") or {}).get("id") or "")
    return {
        # Deterministic and content-derived: the same options produce the same
        # ref, so a React key and a test assertion both stay stable.
        "recommendation_ref": f"{connector_id}:{account_id}:{report_id}",
        "confidence": {"level": confidence, "rationale": rationale},
        "source_account_ref": account_id,
        "source_account_label": str(account.get("label") or "Unlabelled source account"),
        "connector_ref": connector_id,
        "connector_display_name": connector_display_name,
        "connector_contract_version_ref": contract_version_ref,
        "report_ref": report_id,
        # SAME JOIN AS THE CATALOGUE, and that is the point: the missing name is
        # a class with two sites, not a bug with one. Repairing only the
        # catalogue would have left every recommendation card carrying an
        # identifier for 131 reports out of 133.
        "report_display_name": str(
            report.get("display_name") or profile.get("display_name") or report_id
        ),
        # Evidence to READ, not values that get written on click.
        "derived_grain": [str(item) for item in candidate.get("grain") or []],
        "metric_count": len(candidate.get("metrics") or []),
        "dimension_count": len(candidate.get("dimensions") or []),
        "currency": str(candidate.get("currency") or "unknown"),
        "estimated_cost": candidate.get("estimated_cost"),
        "evidence_refs": [
            {
                "kind": "connector_contract",
                "object_type": "connector",
                "object_id": connector_id,
                "version_id": contract_version_ref,
                "fingerprint": contract_fingerprint,
                "observed_at": observed_at,
            },
            {
                "kind": "observed_metadata",
                "object_type": "source-account",
                "object_id": account_id,
                "version_id": "unavailable",
                "fingerprint": str((account.get("evidence") or {}).get("used_by_count") or 0),
                "observed_at": account.get("evidence_as_of") or observed_at,
            },
        ],
    }


def connectors_opened_by_account(
    conn,
    *,
    project_id: str,
    account: dict[str, Any],
    actor: str,
    cache: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Which Connectors the authorization behind this Source Account opens.

    A Source Account is a scope of an AUTHORIZATION, and one authorization does
    not necessarily serve one Connector: a Google direct grant is stored with
    `provider='google'` and opens Search Console, Analytics, Ads and Sheets --
    `connection_tools.list_connection_connectors` is where that mapping already
    lives, scope by scope.

    Everything that asked the question here asked it as
    `account.connector_ref.id == connector_id`, comparing the PROVIDER string to
    a CONNECTOR id. That test is false for every Google direct account, which is
    the whole Google-first stack: no recommendation could be produced for any of
    them, and the wizard offered no way to tell which Connector an account
    belonged to. For a Nango connection the two spellings coincide, so the
    comparison looked correct wherever it was exercised.

    Fails soft: an authorization this actor may not read, or a deployment where
    the module is not installed, yields [] rather than an error -- the step still
    lists the account, it just cannot narrow the Connector for it.
    """
    # When discovery recorded WHICH Connector returned this scope (migration
    # 210), that is the answer and it is exact: a Search Console site is not a
    # candidate for the nine other Connectors the same consent opens.
    discovered_for = str(account.get("discovered_for_connector") or "")
    connection_ref_id = str((account.get("connection_ref") or {}).get("id") or "")
    if not connection_ref_id:
        return []
    if discovered_for:
        authorized = _authorization_connectors(
            conn,
            project_id=project_id,
            connection_ref_id=connection_ref_id,
            actor=actor,
            cache=cache,
        )
        # An empty list means the authorization could not be READ, not that it
        # opens nothing -- the recorded Connector still stands, without claiming
        # an availability nobody verified. A non-empty list that excludes it
        # means the scope was revoked since discovery, and then it opens nothing.
        if not authorized:
            return [
                {
                    "connector_name": discovered_for,
                    "display_name": discovered_for,
                    "available": False,
                    "reason": "This authorization could not be read.",
                }
            ]
        return [
            entry for entry in authorized if entry.get("connector_name") == discovered_for
        ]
    return _authorization_connectors(
        conn,
        project_id=project_id,
        connection_ref_id=connection_ref_id,
        actor=actor,
        cache=cache,
    )


def _authorization_connectors(
    conn,
    *,
    project_id: str,
    connection_ref_id: str,
    actor: str,
    cache: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Every Connector the authorization opens, read once per connection."""
    if connection_ref_id in cache:
        return cache[connection_ref_id]
    opened: list[dict[str, Any]] = []
    try:
        from core.connection_tools import list_connection_connectors  # noqa: PLC0415
        from core.main import get_loaded_modules  # noqa: PLC0415

        opened = [
            connector.as_dict()
            for connector in list_connection_connectors(
                project_id=project_id,
                connection_ref_id=connection_ref_id,
                identity=actor,
                loaded_modules=get_loaded_modules(),
                conn=conn,
            )
        ]
    except Exception:  # noqa: BLE001 -- not-found, unavailable and un-installed are all "cannot narrow"
        opened = []
    cache[connection_ref_id] = opened
    return opened


def _account_opens_connector(opened: list[dict[str, Any]], connector_id: str) -> bool:
    return any(
        entry.get("connector_name") == connector_id and entry.get("available")
        for entry in opened
    )


def account_serves_connector(
    account: dict[str, Any],
    connector_ids: tuple[str, ...] | list[str],
    *,
    opened: list[dict[str, Any]] | None = None,
) -> bool:
    """Does this Source Account serve one of these Connectors? Asked ONCE.

    LA QUESTION EST POSEE PARTOUT ET ELLE N'A QU'UNE BONNE REPONSE. Chaque
    endroit qui l'a posee l'a ecrite `account.connector_ref.id == connector_id`,
    c'est-a-dire en comparant le PROVIDER d'une autorisation a un identifiant de
    Connecteur. Pour un consentement Google direct le provider vaut `"google"`
    (`admin_api.py`, `core/data_surface.py` rend `cr.provider AS connector_id`),
    donc la comparaison est fausse pour les DIX Connecteurs que ce consentement
    ouvre -- toute la pile qui a la priorite. Sur une connexion Nango les deux
    graphies coincident, ce qui est la raison pour laquelle la comparaison a
    l'air juste partout ou on l'exerce.

    La reponse exacte voyage sur `account.opens`
    (`connectors_opened_by_account`). Le provider reste accepte parce qu'il EST
    l'identifiant du Connecteur sur une connexion Nango.

    Ecrit ici, une fois : une reparation posee dans la branche de canal qui l'a
    revelee est une reparation a refaire dix fois.
    """
    names = tuple(connector_ids)
    if str((account.get("connector_ref") or {}).get("id") or "") in names:
        return True
    entries = list((account.get("opens") if opened is None else opened) or [])
    return any(_account_opens_connector(entries, name) for name in names)


def recommend_source_starts(
    conn,
    *,
    project_id: str,
    contracts: list[tuple[Any, ...]],
    source_accounts: list[dict[str, Any]],
    actor: str = "datastream-setup-wizard",
    opened_cache: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Rank (account, connector, report family) starting points for step 1.

    Returns [] rather than a guess whenever the evidence does not support one --
    no exposed account for a persisted contract, no selectable report, no safe
    grain. An empty list is the honest state of a project with nothing
    authorized yet, and the wizard says so instead of showing an empty rail.

    PROJECT INTENT, MEASURED 2026-08-04: the target's phrase is "from project
    intent and observed metadata", but Project-owned intent
    (`project_settings._validate_intent`) is exactly three things -- reporting
    defaults, capability on/off, and selected governed owner ids. None of them
    names a connector or a report, so none of them can rank one. The ranking
    therefore stands on the persisted contract and the observed account
    evidence, and says so on each card rather than implying an input it does
    not have.
    """
    from core.first_report_draft import recommend_first_report  # noqa: PLC0415

    # (sort key, card). The key is kept BESIDE the payload rather than inside
    # it: the account rank belongs to the ordering, not to the read-model, and
    # sorting the payload alone silently re-ordered a stale account ahead of a
    # healthy one by alphabetical label.
    ranked: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    cache = opened_cache if opened_cache is not None else {}
    for row in contracts:
        connector_id, contract_version_ref, contract_fingerprint, snapshot, created_at = (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            row[3],
            row[4],
        )
        capabilities = _raw_source_capabilities(snapshot)
        if not capabilities.get("reports"):
            continue
        reports_by_id = {
            str(report.get("id")): report
            for report in capabilities.get("reports") or []
            if isinstance(report, dict) and report.get("id")
        }
        profiles_by_id = _report_profiles_by_id(snapshot)
        display_name = (
            (_json(snapshot, {}).get("contract") or _json(snapshot, {})).get("display_name")
            or connector_id
        )
        # `None` WHEN NOTHING IS PINNED, and it stays None. A Connector the
        # catalogue offers from its manifest has no observation date, and
        # `str(None)` on an evidence ref would put the word "None" where a
        # reader looks for a timestamp -- a value that is not true, rendered as
        # if it were measured.
        observed_at = (
            created_at.isoformat()
            if hasattr(created_at, "isoformat")
            else (str(created_at) if created_at else None)
        )
        eligible = sorted(
            (
                account
                for account in source_accounts
                if str((account.get("states") or {}).get("availability")) == "available"
                and account_serves_connector(
                    account,
                    (connector_id,),
                    opened=connectors_opened_by_account(
                        conn,
                        project_id=project_id,
                        account=account,
                        actor=actor,
                        cache=cache,
                    ),
                )
            ),
            key=_account_rank,
        )
        for account in eligible[:_MAX_ACCOUNTS_PER_CONNECTOR]:
            recommendation = recommend_first_report(
                conn,
                project_id=project_id,
                capabilities=capabilities,
                account=account,
                actor="datastream-setup-wizard",
                datastream_id=None,
            )
            if recommendation.outcome == "recommended":
                # The engine found exactly ONE safe candidate. That is its own
                # confidence signal, not ours to restate. `as_dict` is used
                # rather than reading the dataclass field by field because it is
                # what already flattens the nested cost/interval records.
                payload = recommendation.as_dict()
                candidates = [
                    {
                        "report_id": payload.get("report_id"),
                        "metrics": payload.get("metrics") or [],
                        "dimensions": payload.get("dimensions") or [],
                        "grain": payload.get("grain") or [],
                        "currency": payload.get("currency"),
                        "estimated_cost": payload.get("estimated_cost"),
                    }
                ]
                confidence, rationale = (
                    "high",
                    "One safe report family in the persisted contract for this account.",
                )
            elif recommendation.candidates:
                candidates = recommendation.candidates
                confidence, rationale = (
                    "medium",
                    "Several report families are compatible; the contract cannot choose for you.",
                )
            else:
                # `no_safe_recommendation` / `no_eligible_account`: the engine
                # declined, and declining is an answer. No card is fabricated.
                continue
            for candidate in candidates:
                card = _recommendation_card(
                    account=account,
                    connector_id=connector_id,
                    connector_display_name=str(display_name),
                    contract_version_ref=contract_version_ref,
                    contract_fingerprint=contract_fingerprint,
                    observed_at=observed_at,
                    candidate=candidate,
                    reports_by_id=reports_by_id,
                    profiles_by_id=profiles_by_id,
                    confidence=confidence,
                    rationale=rationale,
                )
                ranked.append(
                    (
                        (
                            0 if confidence == "high" else 1,
                            str(display_name),
                            _account_rank(account),
                            card["report_ref"],
                        ),
                        card,
                    )
                )

    ranked.sort(key=lambda item: item[0])
    return [card for _key, card in ranked[:_MAX_RECOMMENDATIONS]]


def get_source_options(
    conn, *, project_id: str, draft_id: str, actor: str = "datastream-setup-wizard"
) -> dict[str, Any]:
    """Project the canonical Source Account and persisted contract identities."""
    from core.data_surface import _fetch_rows, project_source_account

    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM app.datastream_setup_drafts WHERE id=%s AND project_id=%s",
            (draft_id, project_id),
        )
        if cur.fetchone() is None:
            raise ObservationNotFound("Draft not found")
        cur.execute(
            "SELECT id,template_code,version,label,grain FROM app.file_source_templates "
            "WHERE project_id=%s AND is_active=TRUE ORDER BY template_code,version DESC",
            (project_id,),
        )
        templates = cur.fetchall()
        # L'ETAT DU CANAL SE LIT, IL NE SE DECLARE PAS.
        #
        # `inbound_email` portait la constante litterale "setup_required", et la
        # fixture du test front affirmait la meme constante. L'installation
        # `managed_feed` est READY et son domaine est configure depuis le
        # 2026-07-25 : l'ecran demandait donc d'aller faire une chose deja faite,
        # et refaire le setup ne pouvait rien y changer -- rien sur ce chemin
        # n'interrogeait la base.
        cur.execute(
            "SELECT c.domain FROM app.connector_domain_configs c "
            "JOIN app.connector_installations i ON i.id=c.installation_id "
            "WHERE c.connector_name='managed_feed' AND c.superseded_by IS NULL "
            "AND i.state IN ('READY','DEGRADED') "
            "ORDER BY c.config_version DESC LIMIT 1"
        )
        inbound_row = cur.fetchone()
    inbound_domain = str(inbound_row[0]) if inbound_row else None
    # THE LENS BY NAME, not by its SQL text (story 59.2): `_fetch_rows` now owns
    # what a lens merges into its rows after the query -- the fleet's issue counts
    # -- so it takes the lens and looks the statement up itself. Passing the query
    # here would silently opt this caller out of that merge, which is how one
    # reader of a read model starts seeing a different object from the others.
    source_rows = _fetch_rows(conn, "sources", project_id)
    source_accounts = [project_source_account(row, project_id=project_id) for row in source_rows]
    # WHICH Connectors each account can serve. Step 1 asks for an account AND a
    # Connector as two independent selects, so nothing stopped a Search Console
    # property being paired with a Meta contract -- a pairing discovery can only
    # refuse later, after the operator has committed to it. Carrying the answer
    # on the account lets the step narrow the second question with the first.
    opened_cache: dict[str, list[dict[str, Any]]] = {}
    for account in source_accounts:
        account["opens"] = connectors_opened_by_account(
            conn, project_id=project_id, account=account, actor=actor, cache=opened_cache
        )
    # THE WHOLE REGISTRY, and `opens` narrows the SECOND question rather than the
    # list. A person with nothing authorized yet still has to see the Connector
    # they came to add; what their authorizations open is a hint carried on each
    # account, not a filter on the catalogue.
    catalogue = registry_connector_catalogue(conn)
    # `recommend_source_starts` ranks (account, connector, report) triples and
    # narrows them by `account_serves_connector`, so recommendations stay bounded
    # by the authorizations that exist while the catalogue is not.
    contracts = [
        (
            entry["connector_ref"],
            entry["contract_version_ref"],
            entry["contract_fingerprint"],
            entry["manifest"],
            entry["observed_at"],
        )
        for entry in catalogue
    ]
    return {
        "draft_ref": draft_id,
        "project_ref": project_id,
        "source_accounts": source_accounts,
        "platform_defaults": PLATFORM_WINDOW_DEFAULTS,
        "connectors": [
            {
                "connector_ref": entry["connector_ref"],
                "contract_version_ref": entry["contract_version_ref"],
                "contract_fingerprint": entry["contract_fingerprint"],
                "display_name": str(
                    entry["manifest"].get("display_name") or entry["connector_ref"]
                ),
                "observed_at": entry["observed_at"],
                # WHICH DOOR OPENS THIS CONNECTOR, said by the manifest that
                # knows. Without it the wizard could only send a person away —
                # "authorizations are created in Data > Sources" — and the
                # screen it sent them to offered one control, wired to Nango,
                # which cannot start the `google_direct` consent the whole
                # Google stack needs. A step that demands an authorization and
                # cannot offer the gesture that makes it is unfinished
                # (CLAUDE.md, "l'ecran"). `auth_type` is a required manifest
                # key, so this is read, never guessed.
                "authorization_kind": str(entry["manifest"].get("auth_type") or "nango"),
                "onboarding_modes": _onboarding_modes(entry["manifest"]),
                **_connector_source_category(entry["connector_ref"]),
                # THE MANIFEST, always: the catalogue says what the module offers
                # today. The pinned snapshot says what a binding committed to,
                # and the difference between them is `contract_state`.
                **_connector_option_contract(
                    entry["manifest"], contract_state=entry["contract_state"]
                ),
            }
            for entry in catalogue
        ],
        "managed_channels": [
            {"channel": "file_upload", "availability": "available", "template_ref": None},
            {"channel": "google_sheets", "availability": "available", "template_ref": None},
            {
                "channel": "inbound_email",
                "availability": "available" if inbound_domain else "setup_required",
                "template_ref": None,
                # Le domaine remonte pour que l'ecran nomme l'endroit ou la
                # livraison arrive au lieu d'un « setup required » sans objet.
                "domain": inbound_domain,
            },
            # `webhook` EST OFFERT depuis la story 57.3 (2026-08-05).
            #
            # Il ne l'etait pas parce que `("managed_feed","channel_contract")`
            # n'avait aucune fonction d'observation, et proposer un canal dont
            # l'etape suivante ne peut rien observer est un cul-de-sac atteint
            # APRES la selection. La condition posee alors -- « il reviendra
            # quand il aura une observation » -- est tenue dans le meme commit :
            # `observe_channel_contract` est sa fonction, et la paire est dans
            # `WIRED_ADAPTERS` ci-dessus.
            #
            # MEME `availability` LUE EN BASE que `inbound_email`, jamais une
            # constante d'ecran : les deux canaux dependent du meme domaine
            # verifie, donc ils ne peuvent pas se contredire.
            {
                "channel": "webhook",
                "availability": "available" if inbound_domain else "setup_required",
                "template_ref": None,
                "domain": inbound_domain,
            },
        ]
        + [
            {
                "channel": "file_upload",
                "availability": "available",
                "template_ref": row[0],
                "template_code": row[1],
                "template_version": int(row[2]),
                "display_name": row[3] or row[1],
                "grain": row[4],
            }
            for row in templates
        ],
        "external_access": _external_access_options(source_accounts, source_rows),
        # Step 1's `Recommended` (`datastream-workbench-and-wizard.md:48`). It
        # rides on the options the step already reads rather than a second
        # round-trip: a recommendation that arrives after the operator has
        # already picked is not a recommendation.
        "recommendations": recommend_source_starts(
            conn,
            project_id=project_id,
            contracts=contracts,
            source_accounts=source_accounts,
            actor=actor,
            opened_cache=opened_cache,
        ),
    }


def _pin_connector_contract(cur, *, connector_ref: str) -> dict[str, Any]:
    """Record what this module declares NOW, because it is being bound now.

    THE BINDING IS THE EVENT, not the deployment. Nothing writes a contract row
    per module ahead of time -- that was 39 rows of data nobody had asked for,
    stale the day a manifest changed and silent about it. A row is written the
    first time a Connector is actually bound, and it is idempotent by
    fingerprint: the second binding of an unchanged module reuses the first
    row rather than adding a version that changed nothing.

    A manifest that does not pass its offline contract check is REFUSED HERE,
    with its issues named. The Connector stays in the catalogue -- hiding it
    would leave the operator looking for something they can see is supported --
    and the refusal lands on the one action it actually blocks.
    """
    from core.context_seed import load_registry_entry  # noqa: PLC0415
    from core.data_identities import (  # noqa: PLC0415
        offline_contract_evidence,
        write_connector_contract_snapshot,
    )

    try:
        entry = load_registry_entry(connector_ref)
    except Exception as exc:  # noqa: BLE001
        raise ObservationNotFound("Connector module is unreadable") from exc
    manifest = (entry or {}).get("manifest")
    if not isinstance(manifest, dict):
        raise ObservationNotFound("Connector module not found in this deployment")
    passed, evidence = offline_contract_evidence(manifest)
    if not passed:
        raise ObservationValidationError(
            "Connector contract cannot be pinned: " + "; ".join(evidence["issues"])[:300]
        )
    recorded = write_connector_contract_snapshot(
        cur,
        environment=deployment_environment(),
        manifest=manifest,
        validation_evidence=evidence,
        # No verification run: there was none, and the column is nullable
        # precisely so a contract does not borrow someone else's evidence.
        verification_run_id=None,
        actor="datastream-setup-wizard",
    )
    from core.data_identities import connector_contract_terms  # noqa: PLC0415

    return {
        "contract": connector_contract_terms(manifest),
        "connector_contract_version_ref": str(recorded["id"]),
    }


def _validate_reference_scope(
    cur, *, project_id: str, draft_id: str, request: dict[str, Any]
) -> dict[str, Any]:
    from core.account_topology import account_connector_sql  # noqa: PLC0415

    mode = request["mode"]
    if mode == "connector_pull":
        cur.execute(
            "SELECT 1 FROM app.projects p JOIN app.connection_ref cr ON cr.owner_org_id=p.org_id "
            "JOIN app.credential_accounts ca ON ca.credential_id=cr.id "
            "WHERE p.id=%s AND ca.source_account_id=%s AND ca.available=TRUE "
            f"AND {account_connector_sql()}",
            (project_id, request["source_account_ref"], [request["connector_ref"]]),
        )
        if cur.fetchone() is None:
            raise ObservationNotFound("Source Account not found")
        version_ref = str(request.get("connector_contract_version_ref") or "")
        if version_ref:
            cur.execute(
                "SELECT contract_snapshot FROM app.connector_contract_versions "
                "WHERE id=%s AND connector_id=%s",
                (version_ref, request["connector_ref"]),
            )
            contract = cur.fetchone()
            if contract is None:
                raise ObservationNotFound("Connector contract not found")
            return {
                "contract": _json(contract[0], {}),
                "connector_contract_version_ref": version_ref,
            }
        return _pin_connector_contract(cur, connector_ref=str(request["connector_ref"]))
    if mode == "external_bq":
        cur.execute(
            "SELECT 1 FROM app.projects p JOIN app.connection_ref cr ON cr.owner_org_id=p.org_id "
            "JOIN app.credential_accounts ca ON ca.credential_id=cr.id "
            "WHERE p.id=%s AND ca.source_account_id=%s AND ca.available=TRUE "
            f"AND {account_connector_sql()}",
            (project_id, request["access_ref"], list(BIGQUERY_CONNECTORS)),
        )
        if cur.fetchone() is None:
            raise ObservationNotFound("BigQuery access not found")
        return {}
    channel = request["channel"]
    if channel == "file_upload":
        cur.execute(
            "SELECT storage_ref,detected_format FROM app.datastream_setup_assets "
            "WHERE id=%s AND draft_id=%s AND project_id=%s "
            "AND state='available' AND expires_at>NOW()",
            (request.get("staged_asset_ref"), draft_id, project_id),
        )
        asset = cur.fetchone()
        if asset is None:
            raise ObservationNotFound("Staged asset not found")
        return {"storage_ref": asset[0], "detected_format": asset[1]}
    if channel == "google_sheets":
        if not request.get("source_account_ref") or not request.get("sheet_ref"):
            raise ObservationValidationError(
                "Google Sheets requires source_account_ref and sheet_ref"
            )
        cur.execute(
            "SELECT 1 FROM app.projects p JOIN app.connection_ref cr ON cr.owner_org_id=p.org_id "
            "JOIN app.credential_accounts ca ON ca.credential_id=cr.id "
            "WHERE p.id=%s AND ca.source_account_id=%s AND ca.available=TRUE "
            f"AND {account_connector_sql()}",
            (project_id, request["source_account_ref"], ["google-sheets"]),
        )
        if cur.fetchone() is None:
            raise ObservationNotFound("Google Sheets Source Account not found")
    return {}


def create_observation(
    conn,
    *,
    project_id: str,
    draft_id: str,
    actor: str,
    idempotency_key: str,
    expected_revision: int,
    request: dict[str, Any],
    adapter: Adapter | None = None,
) -> dict[str, Any]:
    """Create evidence and atomically attach it to one new exact draft revision."""
    from core.data_identities import mint_data_id

    normalized_request = validate_discovery_request(
        {**request, "expected_revision": expected_revision}
    )
    request_fingerprint = _hash(normalized_request)
    key_hash = _hash(idempotency_key)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT d.current_revision_id,r.revision_number,r.normalized_operator_input,d.state "
            "FROM app.datastream_setup_drafts d "
            "JOIN app.datastream_setup_draft_revisions r ON r.id=d.current_revision_id "
            "WHERE d.id=%s AND d.project_id=%s FOR UPDATE",
            (draft_id, project_id),
        )
        draft = cur.fetchone()
        if draft is None:
            raise ObservationNotFound("Draft not found")
        # UN BROUILLON ABANDONNE N'OBSERVE PLUS (AI-336, 2026-08-31). Terminal
        # veut dire terminal sur toutes les ecritures, pas seulement sur la
        # reprise -- et l'UPDATE plus bas ecrivait `state='draft'`, ce qui
        # l'aurait ramene a la vie a la premiere decouverte.
        from core.datastream_preconfiguration import (  # noqa: PLC0415
            DISCARDED_DRAFT_REFUSAL,
            DRAFT_STATE_DISCARDED,
        )

        if str(draft[3]) == DRAFT_STATE_DISCARDED:
            raise ObservationConflict(DISCARDED_DRAFT_REFUSAL)
        cur.execute(
            "SELECT id,request_fingerprint FROM app.datastream_setup_observations "
            "WHERE draft_id=%s AND idempotency_key_hash=%s",
            (draft_id, key_hash),
        )
        replay = cur.fetchone()
        if replay:
            if replay[1] != request_fingerprint:
                raise ObservationConflict(
                    "Idempotency key was reused with different discovery input"
                )
            return read_observation(
                conn,
                project_id=project_id,
                draft_id=draft_id,
                observation_id=replay[0],
                replay=True,
            )
        if int(draft[1]) != expected_revision:
            raise ObservationConflict("Observation belongs to a superseded draft revision")
        current_input = _json(draft[2], {})
        current_mode = current_input.get("mode")
        if current_mode and current_mode != normalized_request["mode"]:
            raise ObservationConflict("Discovery mode no longer matches the draft revision")
        scope = _validate_reference_scope(
            cur, project_id=project_id, draft_id=draft_id, request=normalized_request
        )
        if adapter is not None:
            raw_evidence = adapter(deepcopy(normalized_request))
        elif normalized_request["mode"] == "connector_pull":
            from core.inbound_seam import resolve_inbound  # noqa: PLC0415

            observe_connector_contract = resolve_inbound("observe_connector_contract")

            raw_evidence = observe_connector_contract(
                normalized_request, contract=scope["contract"]
            )
        else:
            raw_evidence = uncovered_adapter_evidence(
                normalized_request["mode"], normalized_request["discovery_kind"]
            )
        evidence = normalize_adapter_evidence(raw_evidence)
        observation_id = mint_data_id("dso")
        revision_id = mint_data_id("dsdr")
        attached_input = deepcopy(current_input)
        attached_source = deepcopy(attached_input.get("source") or {})
        attached_source["observation_ref"] = observation_id
        # THE PIN TRAVELS WITH THE DRAFT. The compiler resolves the source's
        # contract by this ref (`datastream_preconfiguration.py`), so a draft
        # that was bound without one -- every draft, now that nothing is
        # pre-written -- has to learn the ref the binding minted, or the step
        # after would look for a contract the binding already chose.
        if scope.get("connector_contract_version_ref"):
            attached_source["connector_contract_version_ref"] = scope[
                "connector_contract_version_ref"
            ]
        attached_input["source"] = attached_source
        from core.datastream_preconfiguration import _audit, _first_incomplete

        attached_revision = expected_revision + 1
        cur.execute(
            "INSERT INTO app.datastream_setup_draft_revisions "
            "(id,draft_id,project_id,revision_number,normalized_operator_input,"
            "first_incomplete_section,invalidation_causes,content_hash,idempotency_key_hash,"
            "change_reason,created_by) VALUES "
            "(%s,%s,%s,%s,%s::jsonb,%s,'[]'::jsonb,%s,%s,'attach_observation',%s)",
            (
                revision_id,
                draft_id,
                project_id,
                attached_revision,
                _canonical(attached_input),
                _first_incomplete(attached_input),
                _hash(attached_input),
                _hash({"attach_observation": key_hash}),
                actor,
            ),
        )
        now = datetime.now(timezone.utc)
        safe = evidence["safe_metadata"]
        staged_asset = safe.get("staged_asset_ref")
        cur.execute(
            "INSERT INTO app.datastream_setup_observations "
            "(id,project_id,draft_id,draft_revision_id,draft_revision,mode,discovery_kind,"
            "adapter_ref,connector_contract_version_ref,request_fingerprint,evidence_fingerprint,"
            "schema_hash,safe_metadata,coverage,exceptions,staged_asset_id,idempotency_key_hash,"
            "observed_at,created_by) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s)",
            (
                observation_id,
                project_id,
                draft_id,
                revision_id,
                attached_revision,
                normalized_request["mode"],
                normalized_request["discovery_kind"],
                evidence["adapter_ref"],
                # THE REF THE SCOPE RESOLVED, not the one the browser sent. When
                # the request carried none, `_validate_reference_scope` pinned
                # the module's contract in this same transaction and the
                # observation must point at THAT row -- writing back the empty
                # string would leave the binding with no contract recorded.
                scope.get("connector_contract_version_ref")
                or normalized_request.get("connector_contract_version_ref"),
                request_fingerprint,
                evidence["evidence_fingerprint"],
                safe.get("schema_hash"),
                _canonical(safe),
                _canonical(evidence["coverage"]),
                _canonical(evidence["exceptions"]),
                staged_asset,
                key_hash,
                now,
                actor,
            ),
        )
        cur.execute(
            # UNE DECOUVERTE NE TOUCHE PLUS L'ETAT DU TOUT (AI-336, 2026-08-31).
            # La premiere livraison s'observe APRES la materialisation, par
            # construction : une adresse entrante ne s'emet que contre un
            # Datastream materialise. Forcer 'draft' ici violait
            # `datastream_setup_drafts_check` (2026-08-08), et la clause
            # conditionnelle qui l'avait repare ne pouvait plus rien faire
            # d'autre que RESSUSCITER un brouillon abandonne.
            # Seuls les pointeurs de revision et de proposition bougent.
            "UPDATE app.datastream_setup_drafts "
            "SET current_revision_id=%s,current_proposal_id=NULL,"
            "updated_at=NOW() WHERE id=%s AND project_id=%s",
            (revision_id, draft_id, project_id),
        )
        _audit(
            conn,
            actor=actor,
            action=ACTION_DATASTREAM_SETUP_OBSERVATION_CREATED,
            project_id=project_id,
            draft_id=draft_id,
            evidence_hash=evidence["evidence_fingerprint"],
        )
        cur.execute(_OBSERVATION_SELECT, (observation_id, draft_id, project_id))
        return _observation_payload(cur.fetchone())


def read_observation(
    conn,
    *,
    project_id: str,
    draft_id: str,
    observation_id: str,
    replay: bool = False,
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(_OBSERVATION_SELECT, (observation_id, draft_id, project_id))
        row = cur.fetchone()
    if row is None:
        raise ObservationNotFound("Observation not found")
    return _observation_payload(row, replay=replay)


def load_compiler_observation(
    conn, *, project_id: str, draft_id: str, revision_id: str, observation_id: str
) -> dict[str, Any]:
    """Resolve evidence still valid at the given draft revision.

    The observation stays bound to the revision its attachment minted -- the row
    is immutable by trigger -- but the draft head MOVES without invalidating the
    evidence: a `wizard_state`-only PATCH (navigation, autosave of the resume
    marker) mints a revision whose `invalidation_causes` is empty by
    construction (`_invalidation` never looks at `wizard_state`). Requiring
    `draft_revision_id = current_revision_id` therefore orphaned the evidence on
    EVERY post-discovery navigation: observe at rev N, "Continue to configure"
    minted rev N+1, and compile at rev N+1 answered 404 for every mode.

    The rule is the one the revision chain already records: the evidence covers
    the discovery inputs -- `mode` and `source` -- and stays compatible until a
    later revision declares one of those changed. `configure` does NOT appear:
    selecting metrics after discovery is the normal flow, not a re-discovery.
    """
    with conn.cursor() as cur:
        cur.execute(_OBSERVATION_SELECT, (observation_id, draft_id, project_id))
        row = cur.fetchone()
        if row is None:
            raise ObservationNotFound("Compatible observation not found")
        cur.execute(
            "SELECT revision_number FROM app.datastream_setup_draft_revisions "
            "WHERE id=%s AND draft_id=%s AND project_id=%s",
            (revision_id, draft_id, project_id),
        )
        target = cur.fetchone()
        if target is None or int(row[4]) > int(target[0]):
            raise ObservationNotFound("Compatible observation not found")
        cur.execute(
            "SELECT invalidation_causes FROM app.datastream_setup_draft_revisions "
            "WHERE draft_id=%s AND project_id=%s AND revision_number>%s "
            "AND revision_number<=%s",
            (draft_id, project_id, int(row[4]), int(target[0])),
        )
        for (causes,) in cur.fetchall():
            for cause in _json(causes, []):
                if isinstance(cause, dict) and cause.get("dependency") in ("mode", "source"):
                    raise ObservationNotFound("Compatible observation not found")
    payload = _observation_payload(row)
    return {
        "object_id": payload["observation_ref"],
        "version_id": payload["observation_ref"],
        "fingerprint": payload["evidence_fingerprint"],
        "observed_at": payload["observed_at"],
        "safe_metadata": payload["safe_metadata"],
    }
