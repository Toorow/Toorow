"""A publication must never write a downstream-consumer relationship.

Ruling of 2026-08-12 (visual review #69), carried by
`docs/product-architecture/data.md` (« Neither pointer writes a consumer ») and
by the `Incomplete if` of
`docs/product-architecture/datastream-workbench-and-wizard.md`.

`app.datastream_output_used_by` says *this consumer read this Output version*.
A publication knows what it SERVED and cannot know who will READ it, so the row
belongs to the consuming surface (Analyze) and to no publication code. Two
properties of the row settle it: `owner_href` is `NOT NULL` and is the
CONSUMER's console address, and the row can never be retracted -- the migration
138 trigger refuses UPDATE and DELETE, so the row is an observation and not a
declaration of an audience.

These are source-text guards on purpose. The defect they prevent is not a wrong
value at runtime; it is the row appearing at all, written by the wrong hand,
which would fill the Outputs panel with consumers no consumer ever vouched for.
A behavioural test cannot fail on code that must never be written.
"""

from __future__ import annotations

import re
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[2]
CORE = SERVER_ROOT / "core"

# The two publication acts. `publish_activate_mutation` advances the data
# candidate; `_advance_pointer` advances the governed mapping pointer.
PUBLICATION_MODULES = (
    CORE / "datastream_activation.py",
    CORE / "governed_publication.py",
    CORE / "datastream_publication.py",
)

_WRITE = re.compile(
    r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(app\.)?datastream_output_used_by",
    re.IGNORECASE,
)


def _python_sources() -> list[Path]:
    return [p for p in SERVER_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_publication_module_writes_a_consumer_row() -> None:
    """Neither pointer's act may write, update or delete a consumer row."""
    offenders = []
    for path in PUBLICATION_MODULES:
        assert path.exists(), f"publication module moved: {path}"
        if _WRITE.search(path.read_text(encoding="utf-8")):
            offenders.append(path.name)
    assert not offenders, (
        "A publication wrote a downstream-consumer relationship in "
        f"{offenders}. A publication knows what it served and cannot know who "
        "read it -- the row belongs to the consuming surface (Analyze). See "
        "docs/product-architecture/data.md, 'Neither pointer writes a consumer'."
    )


#: The ONE module that earned the write, 2026-08-17. This test was repo-wide
#: until Analyze recorded what it reads; its instruction was to NARROW rather
#: than delete, and this constant is the narrowing. Everything the guard was
#: protecting still holds -- the publication path above is still refused
#: outright, and any third module appearing here still fails.
CONSUMING_MODULES = frozenset({str(Path("core") / "query_execution.py")})


def test_only_the_consuming_module_writes_a_consumer_row() -> None:
    """The row is written by the consumer, and by nothing else. Ruling 2026-08-12.

    Narrowed from "nobody writes it at all" on 2026-08-17, when
    `query_execution.record_output_consumption` landed. The check the previous
    docstring asked for at this moment, made explicit and asserted below rather
    than trusted:

      * the writer sits AT THE READ -- it is called from `run_execution`, on the
        plan `resolve_physical_plan` just produced;
      * it carries the consumer's own identity (`consumer_ref` = the Result id)
        and an `owner_href` addressing the Result;
      * it is idempotent, because the table's trigger refuses UPDATE and DELETE,
        so a retried execution must conflict into nothing.
    """
    writers = {
        str(p.relative_to(SERVER_ROOT))
        for p in _python_sources()
        if _WRITE.search(p.read_text(encoding="utf-8", errors="ignore"))
        and "tests" not in p.parts
    }
    assert writers == CONSUMING_MODULES, (
        f"{sorted(writers)} write app.datastream_output_used_by; only "
        f"{sorted(CONSUMING_MODULES)} may. The consumer records what it read -- "
        "the publication path never does (ruling 2026-08-12)."
    )

    source = (SERVER_ROOT / "core" / "query_execution.py").read_text(encoding="utf-8")
    assert "ON CONFLICT (output_version_id, consumer_kind, consumer_ref)" in source, (
        "the consumption write is not idempotent on the table's primary key, and "
        "an execution is retried"
    )
    assert "DO NOTHING" in source, (
        "the table's trigger refuses UPDATE: `DO UPDATE` would abort the Result"
    )
    assert "owner_href" in source, "the consumption row must address its consumer"


def test_the_panel_still_reads_the_table() -> None:
    """The three readers must survive, or the guards above pass vacuously.

    If the table were dropped, every assertion in this file would go green while
    the Outputs panel lost its subject. These three reads are the measurement
    that the capability is merely unwritten, not withdrawn.
    """
    readers = {
        str(p.relative_to(SERVER_ROOT))
        for p in _python_sources()
        if "datastream_output_used_by" in p.read_text(encoding="utf-8", errors="ignore")
        and p.parts[0] != "tests"
        and "tests" not in p.parts
    }
    assert readers == {
        str(Path("core") / "datastream_workbench.py"),
        str(Path("core") / "capability_proposals.py"),
        # The writer names the table too, and it is the point: from 2026-08-17
        # the panel's subject is no longer structurally empty.
        str(Path("core") / "query_execution.py"),
    }, f"the readers of the consumer table changed: {sorted(readers)}"


def test_delivery_is_an_output_kind_and_never_a_consumer_this_product_serves() -> None:
    """`delivery` is a fourth enum value with no object, producer or consumer.

    Migration 138 allows `consumer_kind = 'delivery'`, but nothing in the
    product delivers a published Output: no `output_plan` ever declares one, so
    the value can never be reached from here either.
    """
    projection = (CORE / "datastream_projection.py").read_text(encoding="utf-8")
    assert "delivery" not in projection, (
        "datastream_projection.py now mentions a delivery. If a compiled "
        "projection can emit a delivery Output, the Workbench document's "
        "'named and inert' ruling for the fourth consumer_kind must be revisited."
    )
