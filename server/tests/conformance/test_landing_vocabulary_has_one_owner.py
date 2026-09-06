"""The landing vocabulary is declared once, and every manifest stays inside it.

Story 64.12 (AI-232). The routing decision "where does this profile land" was
written down TWICE: as an `enum` in `core/schemas/manifest.schema.json`, which the
loader enforces on every module, and as a literal tuple inside
`core.context_events.resolve_landing`.

Two copies of one fact do not stay equal. The failure mode was not the one the
epic first claimed -- a manifest cannot smuggle an unknown landing past the
loader -- it was the reverse: extend the schema enum and `resolve_landing` would
have DEGRADED the new landing to `fact_daily_kpi`, routing a whole destination
into the fact table with no error anywhere. Its caller in `core.queue` wraps the
call in `except Exception -> logger.warning`, so even a raise would have been
swallowed into a line nobody reads.

`resolve_landing` now reads the schema. These tests hold that it keeps doing so.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[2] / "core"
_MODULES = Path(__file__).resolve().parents[2] / "modules"


def _schema_landings() -> set[str]:
    schema = json.loads((_CORE / "schemas" / "manifest.schema.json").read_text(encoding="utf-8"))
    return set(
        schema["properties"]["report_profiles"]["items"]["properties"]["landing"]["enum"]
    )


def test_the_function_reads_the_schema_rather_than_repeating_it():
    """One owner. If these ever differ, routing and validation disagree."""
    from core.context_events import known_landings

    assert known_landings() == _schema_landings()


def test_no_literal_landing_tuple_survives_in_the_router():
    """The copy is gone from the code, not merely shadowed by the new reader.

    A leftover literal is worse than the original defect: it looks authoritative,
    and the next reader cannot tell which of the two the function uses.
    """
    source = (_CORE / "context_events.py").read_text(encoding="utf-8")
    body = source[source.index("def resolve_landing("):]
    body = body[: body.index("\ndef ", 1)] if "\ndef " in body[1:] else body
    assert '("fact_daily_kpi", "context_events")' not in body
    assert "known_landings()" in body


def test_every_declared_landing_is_in_the_vocabulary():
    """The 39 shipped manifests, read from disk, not from a fixture.

    The loader already refuses an unknown value, so this is the cheap early
    catch: it names the offending file at local-test time instead of at module
    load, and it is the one assertion that would fire if the enum were ever
    NARROWED under manifests that already use the removed value.
    """
    known = _schema_landings()
    offenders: list[str] = []
    for manifest_path in sorted(_MODULES.glob("*/manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for profile in manifest.get("report_profiles") or []:
            landing = profile.get("landing")
            if landing is not None and landing not in known:
                offenders.append(
                    f"{manifest_path.parent.name}/{profile.get('id')}: {landing!r}"
                )
    assert not offenders, "landings outside the declared vocabulary: " + ", ".join(offenders)


def test_an_absent_landing_still_derives_and_is_not_refused():
    """Most manifests declare none, and deriving is the designed path.

    Turning absence into a refusal would break every connector that never needed
    to say where it lands -- the regression this change is most likely to cause.
    """
    from core.context_events import resolve_landing

    assert resolve_landing({}, "context") == "context_events"
    assert resolve_landing({}, "kpi") == "fact_daily_kpi"
    assert resolve_landing({}, None) == "fact_daily_kpi"


def test_a_declared_landing_still_wins_over_the_module_kind():
    """The mixed connector Epic 31 exists for: an event profile on a kpi module."""
    from core.context_events import resolve_landing

    assert resolve_landing({"landing": "context_events"}, "kpi") == "context_events"
    assert resolve_landing({"landing": "fact_daily_kpi"}, "context") == "fact_daily_kpi"


def test_the_reader_refuses_a_schema_that_stopped_declaring_the_vocabulary(monkeypatch):
    """A missing enum is a repository defect, not a licence to invent a default.

    Falling back to a literal here would silently restore the second copy this
    story removed, and nobody would learn that the schema had lost its enum.
    """
    from core import context_events

    monkeypatch.setattr(context_events, "_LANDINGS", None)
    monkeypatch.setattr(
        Path, "read_text", lambda self, **kw: json.dumps({"properties": {}})
    )
    with pytest.raises(RuntimeError, match="no landing enum"):
        context_events.known_landings()
    monkeypatch.setattr(context_events, "_LANDINGS", None)
