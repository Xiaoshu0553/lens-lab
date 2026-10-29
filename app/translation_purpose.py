"""Keep presentation samples distinct from research evidence in one database.

Presentation mode is a deployment setting, not a participant-controlled field.
It does not select a model, manufacture a score, or change a translation prompt.
Older translation rows without a purpose remain research rows so enabling the
presentation view never silently relabels existing collected data.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal


RESEARCH_PURPOSE = "research"
DEMONSTRATION_PURPOSE = "demonstration"
DataPurpose = Literal["research", "demonstration"]


def data_purpose(settings: Any) -> DataPurpose:
    """Choose a purpose only from the validated server-side deployment flag."""
    return (DEMONSTRATION_PURPOSE
            if getattr(settings, "translation_showcase_mode", False) is True
            else RESEARCH_PURPOSE)


def purpose_metadata(settings: Any) -> dict:
    """Machine-readable purpose, separate from the participant-facing layout."""
    purpose = data_purpose(settings)
    return {
        "data_purpose": purpose,
        "demonstration_mode": purpose == DEMONSTRATION_PURPOSE,
    }


def record_purpose(record: Mapping) -> DataPurpose | None:
    """Read immutable row provenance, defaulting only genuinely older rows.

    Explicit invalid/null values are not treated as research evidence. This
    fail-closed behavior prevents a damaged provenance field entering either
    population's statistics.
    """
    value = record.get("data_purpose", RESEARCH_PURPOSE)
    return value if value in (RESEARCH_PURPOSE, DEMONSTRATION_PURPOSE) else None


def select_purpose_records(records: Iterable[Mapping], purpose: DataPurpose) -> list:
    """Select one population without mutating records or their evidence fields.

    Cohort, participant/preview status and completion filters still belong to
    the caller. Purpose alone does not establish analysis eligibility.
    """
    if purpose not in (RESEARCH_PURPOSE, DEMONSTRATION_PURPOSE):
        raise ValueError("数据用途须为research或demonstration")
    return [record for record in records if record_purpose(record) == purpose]
