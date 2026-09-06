"""The optional business context is frozen authority, never a client label."""

from __future__ import annotations

import pytest
from core.multi_source_plan import PlanRefused, _compile_analysis_context


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        if "golden_question_versions" in sql:
            self.row = self.rows.get("golden")
        elif "mdm_business_domain_versions" in sql:
            self.row = self.rows.get("domain")
        elif "procedures_versions" in sql:
            self.row = self.rows.get((str(params[0]), int(params[1])))
        else:  # pragma: no cover - a new authority must be named by a test
            raise AssertionError(sql)

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cursor(self.rows)


def test_context_freezes_matching_domain_question_and_requested_skill() -> None:
    conn = _Connection(
        {
            "golden": ("gq_1", "gqv_1", 3, "Revenue by campaign", "a" * 64,
                       "bd_growth", 4, "svv_1"),
            "domain": ("Growth",),
            ("proc_paid_media", 7): ("Paid media investigation",),
        }
    )

    frozen = _compile_analysis_context(
        conn,
        project_id="proj_1",
        semantic_view_version_id="svv_1",
        raw={
            "golden_question_version_id": "gqv_1",
            "skill_version_ids": ["proc_paid_media@7"],
        },
    )

    assert frozen == {
        "contract_version": "analysis-context.v1",
        "semantic_view_version_id": "svv_1",
        "business_domain": {
            "id": "bd_growth",
            "version_number": 4,
            "version_id": "bd_growth:4",
            "name": "Growth",
        },
        "golden_question": {
            "id": "gq_1",
            "version_id": "gqv_1",
            "version_number": 3,
            "title": "Revenue by campaign",
            "content_hash": "a" * 64,
        },
        "requested_skills": [
            {
                "procedure_id": "proc_paid_media",
                "version_number": 7,
                "version_id": "proc_paid_media@7",
                "name": "Paid media investigation",
            }
        ],
    }


def test_question_from_another_semantic_view_is_refused() -> None:
    conn = _Connection(
        {
            "golden": ("gq_1", "gqv_1", 1, "A question", "a" * 64,
                       "bd_1", 1, "svv_other"),
        }
    )
    with pytest.raises(PlanRefused, match="different Semantic View") as exc:
        _compile_analysis_context(
            conn,
            project_id="proj_1",
            semantic_view_version_id="svv_1",
            raw={"golden_question_version_id": "gqv_1"},
        )
    assert exc.value.code == "golden_question_view_mismatch"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ({"business_domain_id": "bd_1"}, "incomplete_business_domain_pin"),
        ({"skill_version_ids": ["latest"]}, "malformed_skill_version_pin"),
        ({"skill_version_ids": ["proc_1@1", "proc_1@1"]}, "duplicate_skill_version_pin"),
        ({"free_text": "growth"}, "undeclared_analysis_context_key"),
    ],
)
def test_context_never_repairs_or_accepts_free_form_authority(raw, code) -> None:
    with pytest.raises(PlanRefused) as exc:
        _compile_analysis_context(
            _Connection({}),
            project_id="proj_1",
            semantic_view_version_id="svv_1",
            raw=raw,
        )
    assert exc.value.code == code
