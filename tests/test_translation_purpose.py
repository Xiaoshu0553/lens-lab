"""Presentation mode must never silently turn generated samples into evidence."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.translation_purpose import (
    DEMONSTRATION_PURPOSE,
    RESEARCH_PURPOSE,
    data_purpose,
    purpose_metadata,
    record_purpose,
    select_purpose_records,
)


def test_presentation_purpose_requires_validated_server_flag():
    assert data_purpose(SimpleNamespace()) == RESEARCH_PURPOSE
    assert data_purpose(SimpleNamespace(translation_showcase_mode=False)) == RESEARCH_PURPOSE
    assert data_purpose(SimpleNamespace(translation_showcase_mode="0")) == RESEARCH_PURPOSE
    assert data_purpose(SimpleNamespace(translation_showcase_mode=True)) == DEMONSTRATION_PURPOSE
    assert purpose_metadata(SimpleNamespace(translation_showcase_mode=True)) == {
        "data_purpose": DEMONSTRATION_PURPOSE,
        "demonstration_mode": True,
    }
    assert purpose_metadata(SimpleNamespace())["data_purpose"] == RESEARCH_PURPOSE


def test_mixed_database_views_preserve_purpose_and_existing_research_rows():
    records = [
        {"id": "older", "record_kind": "participant"},
        {"id": "research", "data_purpose": RESEARCH_PURPOSE, "record_kind": "participant"},
        {"id": "display", "data_purpose": DEMONSTRATION_PURPOSE, "record_kind": "participant"},
        {"id": "preview", "data_purpose": DEMONSTRATION_PURPOSE, "record_kind": "researcher_preview"},
        {"id": "damaged", "data_purpose": None},
        {"id": "unknown", "data_purpose": "unknown"},
    ]
    original = deepcopy(records)
    research = select_purpose_records(records, RESEARCH_PURPOSE)
    display = select_purpose_records(records, DEMONSTRATION_PURPOSE)
    assert [row["id"] for row in research] == ["older", "research"]
    assert [row["id"] for row in display] == ["display", "preview"]
    assert research[0] is records[0] and display[0] is records[2]
    assert records == original
    assert record_purpose(records[-2]) is None
    assert record_purpose(records[-1]) is None
    with pytest.raises(ValueError):
        select_purpose_records(records, "all")
