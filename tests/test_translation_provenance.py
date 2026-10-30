"""Offline source semantics and descriptive checks, not participant evidence."""
from copy import deepcopy

import pytest

from app.translation_provenance import (audit_records, evidence_sha256,
    new_provenance, normalized_provenance)


def test_source_is_independent_of_purpose_and_public_participant_role():
    for purpose in ("demonstration", "research", None):
        row = {"record_kind": "participant", "data_purpose": purpose}
        before = deepcopy(row)
        assert normalized_provenance(row) == new_provenance("participant")
        assert normalized_provenance(row)["collection_method"] == "unverified"
        assert row == before
    assert new_provenance("researcher_preview")["collection_method"] == "researcher_preview"


def test_normalized_provenance_is_defensive_and_does_not_invent_verification_times():
    value = new_provenance()
    value.update(collection_method="automated_script", batch_id="batch-1")
    value["evidence"].append({"type": "manifest", "verified_at": "original-audit-time"})
    row = {"provenance": value}
    result = normalized_provenance(row)
    result["evidence"][0]["verified_at"] = "changed"
    assert row["provenance"]["evidence"][0]["verified_at"] == "original-audit-time"
    assert "verified_at" not in new_provenance()


@pytest.mark.parametrize("source", [None, {}, {"version": 2},
    {"version": 1, "collection_method": "human", "batch_id": None, "evidence": []},
    {"version": 1, "collection_method": [], "batch_id": None, "evidence": []},
    {"version": True, "collection_method": "manual_attested", "batch_id": None, "evidence": []}])
def test_invalid_legacy_metadata_does_not_upgrade_to_a_human_source(source):
    row = {"record_kind": "participant", "provenance": source}
    before = deepcopy(row)
    assert normalized_provenance(row)["collection_method"] == "unverified"
    assert row == before


def test_evidence_digest_covers_all_original_fields_and_only_excludes_provenance():
    row = {"id": "s1", "created_at": "original", "submission": "译文", "answers": {"q": 1},
           "events": [{"type": "reply", "seconds": 1.2}], "assessment": None}
    original = evidence_sha256(row)
    row["provenance"] = new_provenance()
    assert evidence_sha256(row) == original
    for field in ("created_at", "submission", "answers", "events", "assessment"):
        changed = deepcopy(row)
        changed[field] = "different"
        assert evidence_sha256(changed) != original


def test_audit_reports_duplicates_and_actual_time_semantics_without_changing_records():
    row = {"id": "a", "record_kind": "participant", "data_purpose": "demonstration",
           "study_mode": "pilot", "assessment_status": "pending", "submission": "第一句译文。第二句译文。",
           "created_at": "2026-10-30T00:00:00+00:00", "ended_at": "2026-10-30T00:02:00+00:00",
           "events": [{"type": "reply", "seconds": 1.25, "reported_model": "test-cloud-model"}]}
    rows = [row, {**deepcopy(row), "id": "b"},
            {**deepcopy(row), "id": "c", "submission": "第一句译文。 第二句译文。",
             "provenance": {**new_provenance(), "collection_method": "manual_attested"}},
            {"id": "preview", "record_kind": "researcher_preview", "data_purpose": "research"}]
    before = deepcopy(rows)
    report = audit_records(rows)
    assert rows == before
    assert report["counts"]["data_purpose"] == {"demonstration": 3, "research": 1}
    assert report["counts"]["collection_method"] == {"unverified": 2, "manual_attested": 1, "researcher_preview": 1}
    assert report["submissions"]["unique_exact"] == 2
    assert report["submissions"]["exact_duplicate_groups"][0]["session_ids"] == ["a", "b"]
    assert report["submissions"]["whitespace_normalized_duplicate_groups"][0]["count"] == 3
    assert report["submissions"]["similarity"]["pairs_compared"] == 3
    assert report["submissions"]["similarity"]["mean"] == 1
    assert report["api_event_evidence"]["reply_events_with_reported_model"] == 3
    assert report["session_duration_seconds"]["mean"] == 120
    assert "not the whole questionnaire" in report["time_semantics"]["duration"]
    assert "verdicts" in report["interpretation"]["descriptive_only"]
    assert row["submission"] not in str(report)


def test_audit_caps_similarity_and_preserves_unknown_purpose_without_inference():
    rows = [{"id": str(i), "submission": "文字" + str(i)} for i in range(4)]
    report = audit_records(rows, max_similarity_pairs=2)
    assert report["counts"]["data_purpose"] == {"unrecorded": 4}
    assert report["submissions"]["similarity"]["possible_pairs"] == 6
    assert report["submissions"]["similarity"]["pairs_compared"] == 2
    assert report["submissions"]["similarity"]["truncated"] is True
    assert audit_records([])["submissions"]["length_characters"]["mean"] is None
    with pytest.raises(ValueError):
        audit_records([], max_similarity_pairs=-1)


def test_invalid_latencies_and_intervals_are_reported_not_repaired():
    rows = [{"id": "one", "study_mode": "demo", "created_at": "2026-10-30T01:00:00+00:00",
             "ended_at": "2026-10-30T00:00:00+00:00", "events": [
                 {"type": "reply", "seconds": float("inf")}, {"type": "reply", "seconds": -2},
                 {"type": "reply", "seconds": True}, {"type": "model_error"}]}]
    report = audit_records(rows)
    assert report["api_event_evidence"]["reply_events"] == 3
    assert report["api_event_evidence"]["pilot_or_live_reply_events"] == 0
    assert report["api_event_evidence"]["reply_events_with_nonnegative_finite_latency"] == 0
    assert report["session_duration_seconds"]["invalid_intervals"] == 1
    assert rows[0]["events"][0]["seconds"] == float("inf")
