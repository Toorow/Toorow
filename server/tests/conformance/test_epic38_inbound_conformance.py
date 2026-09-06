"""Story 38.19 -- The invariants that hold across the whole inbound connector.

The per-story suites prove their own story. This one proves the properties that
a single story cannot: that no MODULE broke a rule the epic depends on, and that
adding a twelfth module tomorrow does not quietly opt out.

Every check here is written against the class -- the set of inbound modules
discovered from disk -- rather than a hand-maintained list, because a
hand-maintained list is how a new file joins the codebase without joining its
guarantees.

What this file does NOT claim to be: the whole of 38.19. The release gate also
asks for cross-hosting-mode installation evidence, UX acceptance at 400% zoom,
and operations documentation. Those are not here, they are named in the tracker,
and a green run of this file must not be read as a release.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_CORE = _REPO_ROOT / "server" / "core"

#: The inbound surface, discovered rather than listed.
_INBOUND_MODULES = sorted(
    p for p in _CORE.glob("inbound_*.py") if p.name != "__init__.py"
)


def test_the_inbound_surface_is_discovered_and_not_empty():
    """A guard whose subject list is empty passes for the wrong reason."""
    assert len(_INBOUND_MODULES) >= 8, [p.name for p in _INBOUND_MODULES]


# ---------------------------------------------------------------------------
# AD-2: no data-source vendor vocabulary anywhere in core inbound code.
# ---------------------------------------------------------------------------

#: Vendor names that would mean a transport leaked into source-agnostic code.
#: Deliberately data-source and email-provider names -- NOT our own
#: infrastructure (GCS, Pub/Sub), which core legitimately names.
_VENDOR_TOKENS = (
    "mailgun", "sendgrid", "postmark", "mailchimp", "ses.amazonaws",
    "sparkpost", "mandrill",
)


@pytest.mark.parametrize("inbound_module", _INBOUND_MODULES, ids=lambda p: p.name)
def test_no_inbound_module_names_a_transport_vendor(inbound_module):
    text = inbound_module.read_text(encoding="utf-8").lower()
    offenders = [token for token in _VENDOR_TOKENS if token in text]
    assert offenders == [], (
        f"AD-2: {inbound_module.name} names a transport vendor {offenders}. "
        f"Provider vocabulary belongs behind the adapter seam in server/inbound."
    )


# ---------------------------------------------------------------------------
# E38-NFR03: no secret-shaped field can reach a read model.
# ---------------------------------------------------------------------------

#: Field names that must never reach a caller through a SAFE READ MODEL.
_FORBIDDEN_KEYS = (
    "token_hash", "raw_token", "full_secret", "signing_secret",
    "signing_secret_ref", "recipient_address", "raw_address", "plaintext",
)

#: The ONE module allowed to build `full_secret`, and why.
#:
#: Story 38.7 AC2 requires the delivery secret to be shown exactly once, at
#: issuance and rotation. That is a deliberate, bounded disclosure to an
#: authorized human -- not a leak. Pinning it to one module is what makes a
#: SECOND show-once path fail this test instead of shipping.
_SHOW_ONCE_MODULE = "inbound_credentials.py"


def _safe_model_functions(tree: ast.AST):
    """Every function whose job is to build a nondisclosing read model.

    Found by name (`_safe_read_model`, `_safe_*`), because that is the naming
    the inbound modules already share -- and because the alternative, scanning
    every dict in the file, flags the internal row tuples and the audit payload
    that legitimately carry a hash, which is what the first version of this
    test did.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_safe"):
            yield node


@pytest.mark.parametrize("inbound_module", _INBOUND_MODULES, ids=lambda p: p.name)
def test_no_safe_read_model_carries_a_secret_shaped_key(inbound_module):
    """The guard is on the READ MODEL, which is what a caller receives.

    An internal row dict may carry a hash -- it is how the row is loaded -- and
    the operation `request_payload` carries one deliberately. Neither reaches a
    caller. What must never carry one is the function whose entire purpose is to
    be safe to return.
    """
    tree = ast.parse(inbound_module.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for func in _safe_model_functions(tree):
        for node in ast.walk(func):
            if not isinstance(node, ast.Dict):
                continue
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value in _FORBIDDEN_KEYS:
                    offenders.append(f"{func.name}:{key.value}")
    assert offenders == [], (
        f"E38-NFR03: {inbound_module.name} returns {sorted(set(offenders))} from a "
        f"function named as a safe read model."
    )


def test_only_one_module_may_build_a_show_once_secret():
    """A second show-once path is a second place to get it wrong."""
    builders = []
    for module in _INBOUND_MODULES:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value == "full_secret"
                for k in node.keys
            ):
                builders.append(module.name)
                break
    assert set(builders) <= {_SHOW_ONCE_MODULE}, (
        f"`full_secret` is built outside the issuance path: {sorted(set(builders))}"
    )


def test_the_credential_safe_read_model_really_excludes_the_hash():
    """The positive half: not just "no forbidden key", but the right fields.

    An empty or renamed read model would pass the AST check above while
    disclosing nothing AND showing nothing.
    """
    from core.inbound_credentials import _safe_read_model

    model = _safe_read_model(
        credential_id="dic_1",
        datastream_id="ds-1",
        channel="email",
        safe_suffix="aabbcc",
        state="ACTIVE",
        version=1,
        expires_at=None,
        overlap_until=None,
        issued_by="operator",
        created_at=None,
    )
    assert "token_hash" not in model
    assert "full_secret" not in model
    # And it is actually useful.
    assert model["credential_id"] == "dic_1"
    assert model["safe_suffix"] == "aabbcc"


# ---------------------------------------------------------------------------
# AD-28: every inbound WRITE goes through the operation seam.
# ---------------------------------------------------------------------------

_WRITE_SQL = re.compile(
    r"\b(INSERT\s+INTO|UPDATE\s+app\.|DELETE\s+FROM)\b", re.IGNORECASE
)

#: Modules that legitimately contain no SQL write at all.
_READ_ONLY_MODULES = {
    "inbound_health.py",
    "inbound_health_api.py",
    "inbound_mapping_entry.py",
    "inbound_mcp.py",
    "inbound_quarantine.py",
    "inbound_sav.py",
    "inbound_scan.py",
    "inbound_reprocess_api.py",
}


@pytest.mark.parametrize("inbound_module", _INBOUND_MODULES, ids=lambda p: p.name)
def test_a_module_that_writes_also_uses_the_operation_seam(inbound_module):
    """No parallel audit path.

    A module containing a write statement must also reference
    `execute_operation` -- audit and outbox commit with the effect, or the
    effect is unaccounted for.
    """
    text = inbound_module.read_text(encoding="utf-8")
    writes = bool(_WRITE_SQL.search(text))
    if inbound_module.name in _READ_ONLY_MODULES:
        assert not writes, (
            f"{inbound_module.name} is declared read-only but contains a write. "
            f"Either it routes through the operation seam, or the declaration "
            f"is stale -- both need a human, not a silent pass."
        )
        return
    if writes:
        assert "execute_operation" in text, (
            f"AD-28: {inbound_module.name} writes without the operation seam. "
            f"Audit and outbox must commit with the effect."
        )


# ---------------------------------------------------------------------------
# Reachability: a module nothing routes to is not a surface.
# ---------------------------------------------------------------------------


def test_every_inbound_route_family_is_mounted():
    from core.admin_api import router

    mounted = {r.path for r in router.routes if hasattr(r, "path")}
    required = {
        # 38.7
        "/api/connectors/{connector_name}/datastreams/{datastream_id}/credentials",
        # 38.14
        "/api/connectors/{connector_name}/health",
        "/api/connectors/{connector_name}/datastreams/{datastream_id}/inbox",
        "/api/connectors/{connector_name}/datastreams/{datastream_id}"
        "/deliveries/{receipt_id}",
        # 38.16
        "/api/connectors/{connector_name}/datastreams/{datastream_id}"
        "/raw-imports/{raw_import_id}/mapping-context",
        # 38.18
        "/api/connectors/{connector_name}/datastreams/{datastream_id}"
        "/raw-imports/{raw_import_id}/reprocess",
    }
    missing = sorted(required - mounted)
    assert missing == [], f"inbound routes built and mounted nowhere: {missing}"


def test_the_mcp_surface_exposes_no_write():
    """Restated at epic level: the absence is a release property, not a detail."""
    from core.inbound_mcp import INBOUND_MCP_TOOLS

    forbidden = ("execute", "commit", "confirm", "apply", "rotate", "revoke")
    assert not [
        t for t in INBOUND_MCP_TOOLS if any(w in t.lower() for w in forbidden)
    ]


# ---------------------------------------------------------------------------
# Error codes are a contract: stable, and distinct.
# ---------------------------------------------------------------------------


def test_operator_facing_codes_do_not_collide_across_modules():
    """Two modules answering the same string for different causes is a trap.

    An operator matching on `sav_unreadable` must not be reading a quarantine
    failure, and a client rendering a reason must not have to know which module
    produced it.
    """
    from core import inbound_reprocess, inbound_sav, inbound_scan

    buckets = {
        "scan": {
            v for k, v in vars(inbound_scan).items()
            if k.startswith("REASON_") and isinstance(v, str)
        },
        "reprocess": {
            v for k, v in vars(inbound_reprocess).items()
            if k.startswith("UNAVAILABLE_") and isinstance(v, str)
        },
        "sav": {
            v for k, v in vars(inbound_sav).items()
            if k.startswith("SAV_") and isinstance(v, str) and "_" in v
        },
    }
    for left, right in (("scan", "reprocess"), ("scan", "sav"), ("reprocess", "sav")):
        overlap = buckets[left] & buckets[right]
        assert overlap == set(), (
            f"{left} and {right} answer the same code for different causes: {overlap}"
        )
    # And none of them is empty, or the comparison above proves nothing.
    for name, codes in buckets.items():
        assert codes, f"no stable codes discovered for {name}"


# ---------------------------------------------------------------------------
# The honesty invariants this epic keeps having to restate.
# ---------------------------------------------------------------------------


def test_the_scanner_never_reports_a_malware_verdict_it_did_not_get():
    """L'invariant d'honnêteté tient ; ce qui a changé est la conséquence.

    Aucun antivirus n'est câblé dans ce déploiement. La règle était : rien
    n'entre. Elle a été renversée par Jean le 2026-08-05 — ce qui protège une
    ingestion tabulaire est la liste blanche de formats que le fichier vient de
    franchir, pas une signature virale posée derrière elle, et un tableau qu'on
    parse en lignes n'exécute rien.

    L'invariant que ce test porte, lui, ne bouge pas d'un mot : un verdict ne
    rapporte JAMAIS une vérification qu'il n'a pas faite. Sans scanner, le
    verdict est `not_run`, moteur `none`, politique nommée — et surtout pas
    `clean`.
    """
    from core.inbound_scan import MALWARE_POLICY_FORMAT_ALLOWLIST, scan_bytes

    verdict = scan_bytes(b"a,b\n1,2\n")
    assert verdict.accepted is True
    assert verdict.malware == "not_run"
    assert verdict.malware_engine == "none"
    assert verdict.evidence["malware_policy"] == MALWARE_POLICY_FORMAT_ALLOWLIST


def test_an_unreadable_health_layer_is_never_reported_healthy():
    """Same class as a context store answering "nothing found" while it is down."""
    from core.inbound_health import get_inbound_health

    class _Down:
        def cursor(self):
            raise RuntimeError("database unreachable")

    health = get_inbound_health(_Down(), connector_name="c", environment="test")
    assert health["overall"] != "healthy"
    assert all(layer["healthy"] is not True for layer in health["layers"])


def test_a_raw_import_row_cannot_be_created_without_a_content_address():
    """The evidence table is content-addressed, and that is enforced, not hoped."""
    from unittest.mock import MagicMock

    from core.inbound_raw_imports import RawImportValidationError, record_raw_import

    conn = MagicMock()
    for bad_hash in ("", "short", "g" * 64):
        with pytest.raises(RawImportValidationError):
            record_raw_import(
                conn,
                receipt_id="inbrx_1",
                datastream_id="ds-1",
                ordinal=0,
                size_bytes=1,
                content_hash=bad_hash,
                actor="worker",
                idempotency_key="ik-1",
            )
    conn.cursor.assert_not_called()
