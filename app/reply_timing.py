"""Reply-time selection with explicit provenance and reply-weighted summaries."""
from __future__ import annotations

import json
import math


def _detail(event):
    value = event.get("detail", event.get("detail_json"))
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _valid_ms(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def reply_timing(record, events):
    """Prefer measured replies; only explicit authored fixtures use estimates."""
    record = dict(record)
    logged, estimated, versions = [], [], set()
    fixture = (record.get("fixture_conversation") == 1
               and (record.get("is_synthetic") == 1 or record.get("record_kind") == "synthetic"))
    batch = record.get("source_batch_id")
    for raw in events:
        event = dict(raw)
        detail = _detail(event)
        if event.get("event_type") == "reply_latency" and _valid_ms(detail.get("latency_ms")):
            logged.append(detail["latency_ms"])
        elif (fixture and batch and event.get("event_type") == "reply_time_estimate"
              and detail.get("source") == "estimated" and detail.get("is_estimate") is True
              and detail.get("is_synthetic") == 1 and detail.get("source_batch_id") == batch
              and isinstance(detail.get("estimator_version"), str) and detail["estimator_version"]
              and _valid_ms(detail.get("estimated_latency_ms"))):
            estimated.append(detail["estimated_latency_ms"])
            versions.add(detail["estimator_version"])
    samples = logged or estimated
    source = "logged" if logged else "estimated" if estimated else None
    return {
        "mean_reply_seconds": round(sum(samples) / len(samples) / 1000, 2) if samples else None,
        "reply_time_source": source,
        "reply_time_sample_count": len(samples),
        "reply_time_estimator_version": (next(iter(versions)) if len(versions) == 1 else "mixed")
                                        if source == "estimated" else None,
        "reply_time_logged_count": len(logged),
        "reply_time_estimated_count": len(estimated) if not logged else 0,
        "reply_time_sum_ms": sum(samples),
    }


def aggregate_reply_timing(records):
    """Weight by selected replies rather than rounded per-session means."""
    records = list(records)
    logged = sum(record["reply_time_logged_count"] for record in records)
    estimated = sum(record["reply_time_estimated_count"] for record in records)
    count = logged + estimated
    total = sum(record["reply_time_sum_ms"] for record in records)
    versions = {record["reply_time_estimator_version"] for record in records
                if record["reply_time_estimated_count"] and record["reply_time_estimator_version"]}
    return {
        "mean_reply_seconds": round(total / count / 1000, 2) if count else None,
        "reply_time_source": "mixed" if logged and estimated else "logged" if logged else "estimated" if estimated else None,
        "reply_time_sample_count": count,
        "reply_time_estimator_version": next(iter(versions)) if len(versions) == 1 else "mixed" if versions else None,
        "reply_time_logged_count": logged,
        "reply_time_estimated_count": estimated,
    }
