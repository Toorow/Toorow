from __future__ import annotations

import pytest


def test_board_selection_accepts_one_or_many_opaque_ids(connector):
    assert connector._board_ids({"board_id": "9007199254740993"}) == [
        "9007199254740993"
    ]
    assert connector._board_ids({"board_ids": ["b2", "b1", "b2"]}) == ["b2", "b1"]
    with pytest.raises(ValueError):
        connector._board_ids({})
