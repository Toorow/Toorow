"""Terraform inbound outputs must expose no secret / capability (Story 38.1, AC3).

We cannot run `terraform` in the subagent (central verify does that), so this is
a rendered-string / grep-style static assertion over the .tf sources:

  * outputs.tf only exports `inbound_receipt_url` + `inbound_readiness_id`;
  * neither output references the signing secret or any secret_manager value;
  * the inbound_runtime.tf commits no secret VALUE (only references).

A cheap, deterministic guard that complements `terraform validate`.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[3]
_TF_DIR = _REPO_ROOT / "infra" / "terraform"
_OUTPUTS = _TF_DIR / "outputs.tf"
_RUNTIME = _TF_DIR / "inbound_runtime.tf"


def _output_blocks(text: str) -> dict[str, str]:
    """Return {output_name: block_body} for every `output "name" { ... }`."""
    blocks: dict[str, str] = {}
    for m in re.finditer(r'output\s+"([^"]+)"\s*\{', text):
        name = m.group(1)
        start = m.end()
        depth = 1
        i = start
        while i < len(text) and depth > 0:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        blocks[name] = text[start : i - 1]
    return blocks


def test_inbound_outputs_present():
    text = _OUTPUTS.read_text(encoding="utf-8")
    blocks = _output_blocks(text)
    assert "inbound_receipt_url" in blocks
    assert "inbound_readiness_id" in blocks


def _strip_prose(body: str) -> str:
    """Drop HCL comments and `description = "..."` prose.

    AC3 is about the exported `value`, not human documentation. Words like
    "capability" or "datastream" legitimately appear in a description that says
    an output contains *no* capability — that must not trip the guard, while a
    real reference to a secret/capability in `value` still does.
    """
    out: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("#") or line.startswith("//"):
            continue
        line = re.split(r"\s#", raw, maxsplit=1)[0]
        line = re.sub(r'description\s*=\s*".*"', "description = \"\"", line)
        out.append(line)
    return "\n".join(out)


def test_inbound_outputs_reference_no_secret():
    text = _OUTPUTS.read_text(encoding="utf-8")
    blocks = _output_blocks(text)
    inbound_blocks = {
        k: _strip_prose(v) for k, v in blocks.items() if k.startswith("inbound_")
    }
    assert inbound_blocks, "expected inbound_* outputs"

    forbidden = [
        "google_secret_manager_secret.inbound_signing_secret.secret_data",
        "signing_secret",
        "secret_data",
        "secret_key_ref",
        "value_source",
        "capability",
        "ds_token",
        "datastream",
    ]
    for name, body in inbound_blocks.items():
        lowered = body.lower()
        for term in forbidden:
            assert term not in lowered, (
                f"output {name!r} references forbidden token {term!r} — outputs "
                f"must contain no secret and no Datastream capability (AC3)."
            )


def test_runtime_commits_no_secret_value():
    """The signing secret must be a REFERENCE, never an inline value."""
    text = _RUNTIME.read_text(encoding="utf-8")
    # A committed secret would show up as e.g. secret_data = "..." on the secret.
    assert "secret_data" not in text, "no secret VALUE may be committed in Terraform"
    # The secret must be wired as a Secret Manager reference in the container env.
    assert "secret_key_ref" in text, "signing secret must be injected as a reference"
    # Ingress must be public so provider webhooks can reach the service.
    assert "INGRESS_TRAFFIC_ALL" in text
    # Scale-to-zero contract.
    assert "min_instance_count = 0" in text
    # Public access prevention enforced on the quarantine bucket.
    assert 'public_access_prevention    = "enforced"' in text or \
        'public_access_prevention = "enforced"' in text
