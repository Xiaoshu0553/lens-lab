import json

import pytest

from app.reply_timing import aggregate_reply_timing, reply_timing


FIXTURE = {"record_kind": "synthetic", "is_synthetic": 1, "fixture_conversation": 1,
           "source_batch_id": "current-batch"}


def estimate(ms=2500, **overrides):
    return {"event_type": "reply_time_estimate", "detail": {
        "estimated_latency_ms": ms, "source": "estimated", "is_estimate": True,
        "is_synthetic": 1, "source_batch_id": "current-batch",
        "estimator_version": "estimator-v1", **overrides}}


def logged(ms):
    return {"event_type": "reply_latency", "detail_json": json.dumps({"latency_ms": ms})}


def test_real_samples_override_estimates_for_the_entire_session():
    timing = reply_timing(FIXTURE, [estimate(9000), logged(1000), logged(3000)])
    assert timing["mean_reply_seconds"] == 2
    assert timing["reply_time_source"] == "logged"
    assert timing["reply_time_sample_count"] == timing["reply_time_logged_count"] == 2
    assert timing["reply_time_estimated_count"] == 0
    assert timing["reply_time_estimator_version"] is None


def test_missing_measurements_never_fill_ordinary_participant_records():
    timing = reply_timing({"record_kind": "participant"}, [estimate()])
    assert timing["mean_reply_seconds"] is None
    assert timing["reply_time_source"] is None and timing["reply_time_sample_count"] == 0


@pytest.mark.parametrize("overrides", [
    {"source_batch_id": "old-batch"}, {"source": "logged"}, {"is_estimate": False},
    {"is_synthetic": 0}, {"estimator_version": ""}, {"estimator_version": None},
])
def test_estimate_requires_matching_explicit_provenance(overrides):
    assert reply_timing(FIXTURE, [estimate(**overrides)])["mean_reply_seconds"] is None


@pytest.mark.parametrize("value", [True, False, None, "1000", -1, float("nan"), float("inf")])
def test_invalid_timing_samples_are_ignored(value):
    assert reply_timing(FIXTURE, [estimate(value), logged(value)])["mean_reply_seconds"] is None


def test_zero_is_a_valid_measurement_and_invalid_json_is_not_a_sample():
    timing = reply_timing({}, [logged(0), {"event_type": "reply_latency", "detail_json": "broken"}])
    assert timing["mean_reply_seconds"] == 0
    assert timing["reply_time_source"] == "logged" and timing["reply_time_sample_count"] == 1


def test_aggregate_uses_reply_counts_not_a_mean_of_session_means():
    first = reply_timing(FIXTURE, [estimate(1000)])
    second = reply_timing({}, [logged(3000), logged(3000), logged(3000)])
    timing = aggregate_reply_timing([first, second])
    assert timing["mean_reply_seconds"] == 2.5
    assert timing["reply_time_source"] == "mixed"
    assert timing["reply_time_sample_count"] == 4
    assert timing["reply_time_logged_count"] == 3 and timing["reply_time_estimated_count"] == 1
    assert aggregate_reply_timing([])["mean_reply_seconds"] is None
