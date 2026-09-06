"""AD-1 on the three Context Hub discovery tools (live finding A.2).

WHAT WAS WRONG. `get_knowledge`, `get_skills` and `get_context_hub` serialized
their ENTIRE payload into `TextContent` and returned no `structuredContent` at
all: every topic body, every procedure body and frontmatter, the whole taxonomy,
straight onto the LLM channel. A project with fifty governed topics burned the
caller's context window on a call whose whole purpose is to say WHAT EXISTS.

AD-1 splits the two channels: a bounded, readable index for the model, the full
object for the application. `search_context` has done this since Story 11.x; the
three discovery tools are now held to the same contract.

The bound is PROVEN, not asserted: each test grows the corpus by an order of
magnitude and shows the text channel does not grow with it.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

import pytest

_MAX_LINES = 30


def _topic(n: int, *, platform: bool = False) -> dict:
    return {
        "id": f"top_{n:04d}",
        "project_id": None if platform else "proj_EXAMPLE",
        "title": f"Governed topic {n}",
        "body_md": f"UNIQUE_BODY_MARKER_{n} " + ("lorem ipsum " * 200),
        "status": "active",
        "owner": "owner@example.com",
        "created_by": "owner@example.com",
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
        "version_number": 3,
    }


def _procedure(n: int, *, platform: bool = False) -> dict:
    return {
        "id": f"proc_{n:04d}",
        "project_id": None if platform else "proj_EXAMPLE",
        "name": f"skill-{n}",
        "description": f"Does thing {n}.",
        "frontmatter_yaml": f"name: skill-{n}\nsteps:\n" + ("  - step: 1\n" * 50),
        "body_md": f"UNIQUE_BODY_MARKER_{n} " + ("lorem ipsum " * 200),
        "status": "active",
        "owner": "owner@example.com",
        "created_by": "owner@example.com",
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
        "version_number": 2,
    }


@contextlib.contextmanager
def _context_store(topics: list[dict], procedures: list[dict], *, taxonomy=None, links=None):
    from core import main  # noqa: PLC0415

    conn = MagicMock()
    patches = [
        patch("core.main.get_access_token", return_value=None),
        patch("core.main._resolve_project", side_effect=lambda p, identity=None: p or "default"),
        patch("core.main._refuse_unless_project_scope", return_value=None),
        patch("core.db.get_connection", return_value=contextlib.nullcontext(conn)),
        patch("core.context_store.list_topics", return_value=topics),
        patch("core.context_store.list_procedures", return_value=procedures),
        patch("core.context_api._project_org_id", return_value="org_EXAMPLE"),
        patch(
            "core.business_taxonomy.list_taxonomy",
            return_value=taxonomy
            if taxonomy is not None
            else {"org_id": "org_EXAMPLE", "domains": [], "classifications": []},
        ),
        patch("core.business_taxonomy.list_links", return_value=links or []),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield main


def _channels(result):
    return result.content[0].text, result.structured_content


# ---------------------------------------------------------------------------
# get_knowledge
# ---------------------------------------------------------------------------


def test_get_knowledge_indexes_on_the_llm_channel_and_details_on_the_app_channel():
    topics = [_topic(n) for n in range(1, 4)]
    with _context_store(topics, []) as main:
        text, structured = _channels(main.get_knowledge(project_id="proj_EXAMPLE"))

    assert "3 topic(s)" in text
    assert "Governed topic 1" in text          # the index names what exists
    assert "UNIQUE_BODY_MARKER_1" not in text  # the corpus does not ride along
    assert structured is not None, "the detail channel must exist at all"
    assert structured["data"]["count"] == 3
    assert structured["data"]["topics"][0]["body_md"].startswith("UNIQUE_BODY_MARKER_1")
    assert structured["meta"]["provenance"]["source_field"] == "get_knowledge"


def test_get_knowledge_llm_channel_does_not_grow_with_the_library():
    """The bound proven on two inputs that differ ONLY in how many topics."""
    with _context_store([_topic(n) for n in range(1, 4)], []) as main:
        small, _ = _channels(main.get_knowledge(project_id="proj_EXAMPLE"))
    with _context_store([_topic(n) for n in range(1, 201)], []) as main:
        large, structured = _channels(main.get_knowledge(project_id="proj_EXAMPLE"))

    assert len(large.splitlines()) <= _MAX_LINES
    assert len(large) < len(small) * 12   # bounded, not proportional to 200/3
    assert "more in the detail" in large  # and the truncation is SAID
    assert structured["data"]["count"] == 200, "nothing is lost, it moved channel"


def test_get_knowledge_says_an_empty_library_is_empty():
    with _context_store([], []) as main:
        text, structured = _channels(main.get_knowledge(project_id="proj_EXAMPLE"))
    assert "No governed knowledge topic" in text
    assert structured["data"]["count"] == 0


def test_get_knowledge_index_marks_the_platform_scope():
    """AD-5 is the reason a reader cares which rows are platform-wide; the index
    would be misleading without it."""
    with _context_store([_topic(1, platform=True), _topic(2)], []) as main:
        text, _ = _channels(main.get_knowledge(project_id="proj_EXAMPLE"))
    assert "[platform, v3]" in text
    assert "[project, v3]" in text


# ---------------------------------------------------------------------------
# get_skills
# ---------------------------------------------------------------------------


def test_get_skills_indexes_on_the_llm_channel_and_details_on_the_app_channel():
    procedures = [_procedure(n) for n in range(1, 4)]
    with _context_store([], procedures) as main:
        text, structured = _channels(main.get_skills(project_id="proj_EXAMPLE"))

    assert "3 skill(s)" in text
    assert "skill-1" in text
    assert "Does thing 1." in text
    assert "UNIQUE_BODY_MARKER_1" not in text
    assert "steps:" not in text, "the frontmatter is detail, not index"
    assert structured["data"]["count"] == 3
    assert structured["data"]["procedures"][0]["frontmatter_yaml"].startswith("name: skill-1")
    assert structured["meta"]["provenance"]["source_field"] == "get_skills"


def test_get_skills_llm_channel_does_not_grow_with_the_registry():
    with _context_store([], [_procedure(n) for n in range(1, 4)]) as main:
        small, _ = _channels(main.get_skills(project_id="proj_EXAMPLE"))
    with _context_store([], [_procedure(n) for n in range(1, 201)]) as main:
        large, structured = _channels(main.get_skills(project_id="proj_EXAMPLE"))

    assert len(large.splitlines()) <= _MAX_LINES
    assert len(large) < len(small) * 12
    assert structured["data"]["count"] == 200


# ---------------------------------------------------------------------------
# get_context_hub
# ---------------------------------------------------------------------------


def test_get_context_hub_censuses_on_the_llm_channel_and_details_on_the_app_channel():
    taxonomy = {
        "org_id": "org_EXAMPLE",
        "domains": [{"id": "bdm_sales", "slug": "sales", "name": "Sales", "status": "active"}],
        "classifications": [{"id": "bcl_ent", "slug": "enterprise", "name": "Enterprise"}],
    }
    with _context_store(
        [_topic(1)], [_procedure(1)], taxonomy=taxonomy, links=[{"id": "lnk_1"}]
    ) as main:
        text, structured = _channels(main.get_context_hub(project_id="proj_EXAMPLE"))

    assert "1 business domain(s)" in text
    assert "1 classification(s)" in text
    assert "1 governed link(s)" in text
    assert "1 knowledge topic(s)" in text
    assert "1 skill(s)" in text
    assert "UNIQUE_BODY_MARKER_1" not in text
    assert structured["data"]["taxonomy"] == taxonomy
    assert structured["data"]["topics"][0]["body_md"].startswith("UNIQUE_BODY_MARKER_1")
    assert structured["meta"]["provenance"]["source_field"] == "get_context_hub"


def test_get_context_hub_llm_channel_does_not_grow_with_the_hub():
    big = {
        "org_id": "org_EXAMPLE",
        "domains": [
            {"id": f"bdm_{n}", "slug": f"domain-{n}", "name": f"Domain {n}"} for n in range(200)
        ],
        "classifications": [{"id": f"bcl_{n}"} for n in range(500)],
    }
    with _context_store([_topic(n) for n in range(60)], [_procedure(n) for n in range(60)],
                        taxonomy=big, links=[{"id": f"lnk_{n}"} for n in range(300)]) as main:
        text, structured = _channels(main.get_context_hub(project_id="proj_EXAMPLE"))

    assert len(text.splitlines()) <= _MAX_LINES
    assert structured["data"]["domains_count"] == 200
    assert structured["data"]["classifications_count"] == 500
    assert structured["data"]["links_count"] == 300


def test_get_context_hub_reads_the_projects_own_organization():
    """`org_id="org_default"` was hard-coded here, so every project outside that
    one organization read a taxonomy and a link set that were not its own -- an
    AD-5 scope break wearing the clothes of a default value."""
    with _context_store([], []) as main:
        with patch("core.business_taxonomy.list_taxonomy") as tax_mock, patch(
            "core.business_taxonomy.list_links", return_value=[]
        ) as links_mock:
            tax_mock.return_value = {"org_id": "org_EXAMPLE", "domains": [], "classifications": []}
            result = main.get_context_hub(project_id="proj_EXAMPLE")

    assert tax_mock.call_args.kwargs["org_id"] == "org_EXAMPLE"
    assert links_mock.call_args.kwargs["org_id"] == "org_EXAMPLE"
    assert result.structured_content["data"]["org_id"] == "org_EXAMPLE"


# ---------------------------------------------------------------------------
# La branche PAR ID -- elle levait un AttributeError a chaque appel (2026-08-10)
# ---------------------------------------------------------------------------
#
#     cd server && python -c "from core import context_store
#       for n in ('get_topic_by_id','get_procedure_by_id'):
#           print(n, '->', hasattr(context_store, n))"
#       get_topic_by_id     -> False
#       get_procedure_by_id -> False
#
# `main.py` appelait ces deux noms depuis `get_knowledge(topic_id=...)` et
# `get_skills(procedure_id=...)`. Aucun n'existe : une branche entiere de l'API
# de connaissance levait, et AUCUN test ne la parcourait. Les lecteurs qui
# existent sont `get_topic` / `get_procedure`, et ils prennent
# `caller_project_id`.
#
# CES TESTS PARCOURENT LE VRAI LECTEUR. `context_store` n'est pas moque ici :
# seule la connexion l'est, et elle rend la ligne demandee -- le refus de portee
# d'AD-5 est un predicat PYTHON (`context_store.py:876-882`, `:1270-1276`), donc
# c'est bien lui qui decide, pas le faux.


class _ByIdCursor:
    """Rend UNE ligne pour la lecture par id, avec sa `description`."""

    def __init__(self, row, columns):
        self._row, self._columns = row, columns

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    @property
    def description(self):
        return [(name,) for name in self._columns]

    def execute(self, _sql, _params=None):
        return None

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [self._row] if self._row else []


class _ByIdConn:
    def __init__(self, row, columns):
        self._row, self._columns = row, columns

    def cursor(self):
        return _ByIdCursor(self._row, self._columns)


_TOPIC_COLUMNS = (
    "id", "project_id", "title", "body_md", "status", "owner", "created_by",
    "created_at", "updated_at", "version_number",
)
_PROCEDURE_COLUMNS = (
    "id", "project_id", "name", "description", "frontmatter_yaml", "body_md",
    "status", "owner", "created_by", "created_at", "updated_at", "version_number",
)


@contextlib.contextmanager
def _by_id(row, columns):
    from core import main  # noqa: PLC0415

    conn = _ByIdConn(row, columns)
    with contextlib.ExitStack() as stack:
        for p in (
            patch("core.main.get_access_token", return_value=None),
            patch(
                "core.main._resolve_project",
                side_effect=lambda p, identity=None: p or "default",
            ),
            patch("core.main._refuse_unless_project_scope", return_value=None),
            patch("core.db.get_connection", return_value=contextlib.nullcontext(conn)),
            patch("core.db.install_access_context", return_value=None),
        ):
            stack.enter_context(p)
        yield main


def test_asking_for_one_topic_by_id_returns_it_instead_of_raising():
    row = ("top_0001", "proj_EXAMPLE", "Governed topic 1", "the body", "active",
           "owner@example.com", "owner@example.com", "2026-08-01", "2026-08-01", 3)
    with _by_id(row, _TOPIC_COLUMNS) as main:
        text, structured = _channels(
            main.get_knowledge(project_id="proj_EXAMPLE", topic_id="top_0001")
        )
    assert [t["id"] for t in structured["data"]["topics"]] == ["top_0001"]
    assert "top_0001" in text


def test_asking_for_one_skill_by_id_returns_it_instead_of_raising():
    row = ("proc_0001", "proj_EXAMPLE", "skill-1", "Does thing 1.",
           "name: skill-1\n", "the body", "active", "owner@example.com",
           "owner@example.com", "2026-08-01", "2026-08-01", 2)
    with _by_id(row, _PROCEDURE_COLUMNS) as main:
        text, structured = _channels(
            main.get_skills(project_id="proj_EXAMPLE", procedure_id="proc_0001")
        )
    assert [p["id"] for p in structured["data"]["procedures"]] == ["proc_0001"]
    assert "skill-1" in text


@pytest.mark.parametrize(
    ("tool", "argument", "row", "columns", "key"),
    [
        (
            "get_knowledge", "topic_id",
            ("top_0001", "proj_OTHER", "Another project's topic", "body", "active",
             "owner@example.com", "owner@example.com", "2026-08-01", "2026-08-01", 3),
            _TOPIC_COLUMNS, "topics",
        ),
        (
            "get_skills", "procedure_id",
            ("proc_0001", "proj_OTHER", "skill-1", "Another project's skill",
             "name: skill-1\n", "body", "active", "owner@example.com",
             "owner@example.com", "2026-08-01", "2026-08-01", 2),
            _PROCEDURE_COLUMNS, "procedures",
        ),
    ],
)
def test_an_id_from_a_neighbouring_project_returns_nothing_by_id(
    tool, argument, row, columns, key
):
    """Une lecture par id refuse hors perimetre EXACTEMENT comme la lecture par
    liste. Et elle refuse en NE RENDANT RIEN -- ni la ligne, ni un 403 qui
    revelerait que l'id existe ailleurs."""
    with _by_id(row, columns) as main:
        text, structured = _channels(
            getattr(main, tool)(**{"project_id": "proj_EXAMPLE", argument: row[0]})
        )
    assert structured["data"][key] == []
    assert row[0] not in text
    assert "proj_OTHER" not in text


def test_a_platform_node_stays_readable_by_id_from_any_project():
    """Le controle negatif : `project_id IS NULL` est de PLATEFORME et se lit
    partout. Un predicat trop strict le cacherait a tout le monde, ce qui est
    l'autre facon de casser AD-5."""
    row = ("top_0002", None, "Platform topic", "body", "active",
           "owner@example.com", "owner@example.com", "2026-08-01", "2026-08-01", 1)
    with _by_id(row, _TOPIC_COLUMNS) as main:
        _text, structured = _channels(
            main.get_knowledge(project_id="proj_EXAMPLE", topic_id="top_0002")
        )
    assert [t["id"] for t in structured["data"]["topics"]] == ["top_0002"]


@pytest.mark.parametrize("tool_name", ["get_knowledge", "get_skills", "get_context_hub"])
def test_every_discovery_tool_answers_on_both_channels(tool_name):
    """The class, not the instance: a fourth discovery tool added beside these
    three must carry both channels too."""
    with _context_store([_topic(1)], [_procedure(1)]) as main:
        result = getattr(main, tool_name)(project_id="proj_EXAMPLE")

    assert result.content and result.content[0].text.strip()
    assert result.structured_content is not None
    assert "data" in result.structured_content
    assert result.structured_content["meta"]["freshness"] == "live"
    assert len(result.content[0].text.splitlines()) <= _MAX_LINES
