"""Independent collection-source metadata and descriptive data audits.

``data_purpose`` describes intended use, not who operated the platform.  A
participant route, a model response, or a completion time cannot establish that
a human answered a questionnaire.  No helper in this module relabels purpose or
changes any recorded participant evidence.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
from difflib import SequenceMatcher
import hashlib
import json
import math
import re
import statistics
from typing import Iterable, Mapping


PROVENANCE_VERSION = 1
COLLECTION_METHODS = frozenset({
    "automated_script", "manual_attested", "unverified", "researcher_preview",
})


def new_provenance(record_kind: str = "participant") -> dict:
    """Server-only initial metadata; public entry does not attest human origin."""
    return {
        "version": PROVENANCE_VERSION,
        "collection_method": "researcher_preview" if record_kind == "researcher_preview" else "unverified",
        "batch_id": None,
        "evidence": [],
    }


def normalized_provenance(row: Mapping) -> dict:
    """Return a defensive copy; missing/invalid legacy sources stay unverified.

    ``record_kind`` can identify an authenticated preview workflow, but never
    turns an ordinary historical participant record into a human attestation.
    Invalid existing values are not rewritten in storage by this function.
    """
    value = row.get("provenance")
    if (not isinstance(value, Mapping) or type(value.get("version")) is not int
            or value.get("version") != PROVENANCE_VERSION
            or not isinstance(value.get("collection_method"), str)
            or value.get("collection_method") not in COLLECTION_METHODS
            or not isinstance(value.get("evidence"), list)
            or any(not isinstance(item, Mapping) for item in value["evidence"])
            or (value.get("batch_id") is not None and not isinstance(value.get("batch_id"), str))):
        return new_provenance(row.get("record_kind", "participant"))
    return deepcopy(dict(value))


def evidence_projection(row: Mapping) -> dict:
    """All original record fields, excluding only the new provenance namespace."""
    return deepcopy({key: value for key, value in row.items() if key != "provenance"})


def evidence_sha256(row: Mapping) -> str:
    encoded = json.dumps(evidence_projection(row), ensure_ascii=False, sort_keys=True,
                         allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _label(value) -> str:
    return value if isinstance(value, str) and value else "unrecorded"


def _distribution(values: list[float | int]) -> dict:
    if not values:
        return {"count": 0, "minimum": None, "maximum": None, "mean": None,
                "median": None, "population_sd": None}
    return {"count": len(values), "minimum": min(values), "maximum": max(values),
            "mean": round(statistics.mean(values), 6),
            "median": statistics.median(values),
            "population_sd": round(statistics.pstdev(values), 6)}


def _duplicate_groups(pairs: list[tuple[str, str]]) -> list[dict]:
    groups = defaultdict(list)
    for sid, text in pairs:
        groups[hashlib.sha256(text.encode("utf-8")).hexdigest()].append(sid)
    return [{"sha256": digest, "session_ids": sorted(ids), "count": len(ids)}
            for digest, ids in sorted(groups.items()) if len(ids) > 1]


def audit_records(rows: Iterable[Mapping], *, max_similarity_pairs: int = 10_000) -> dict:
    """Describe stored evidence without judging authenticity or modifying rows.

    Similarity is a descriptive whitespace-normalized SequenceMatcher ratio;
    it is neither a translation-quality score nor an origin detector.  The
    deterministic cap is reported explicitly for large collections.
    """
    if type(max_similarity_pairs) is not int or max_similarity_pairs < 0:
        raise ValueError("max_similarity_pairs must be a nonnegative integer")
    records = list(rows)
    purposes, methods, kinds, batches, statuses = (Counter() for _ in range(5))
    submissions, durations = [], []
    reply_count = timed_replies = reported_models = non_demo_replies = model_errors = 0
    invalid_time_rows = 0
    for row in records:
        purposes[_label(row.get("data_purpose"))] += 1
        source = normalized_provenance(row)
        methods[source["collection_method"]] += 1
        kinds[_label(row.get("record_kind"))] += 1
        batches[_label(source.get("batch_id"))] += 1
        statuses[_label(row.get("assessment_status"))] += 1
        text = row.get("submission")
        if isinstance(text, str) and text.strip():
            submissions.append((str(row.get("id", "")), text))
        events = row.get("events", [])
        for event in events if isinstance(events, list) else []:
            if not isinstance(event, Mapping):
                continue
            if event.get("type") == "model_error":
                model_errors += 1
            if event.get("type") != "reply":
                continue
            reply_count += 1
            if row.get("study_mode") in {"pilot", "live"}:
                non_demo_replies += 1
            seconds = event.get("seconds")
            if type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0:
                timed_replies += 1
            if isinstance(event.get("reported_model"), str) and event["reported_model"].strip():
                reported_models += 1
        if row.get("ended_at"):
            try:
                start = datetime.fromisoformat(row["created_at"])
                end = datetime.fromisoformat(row["ended_at"])
                seconds = (end - start).total_seconds()
                if start.tzinfo is None or end.tzinfo is None or seconds < 0:
                    raise ValueError("invalid interval")
                durations.append(seconds)
            except (KeyError, TypeError, ValueError, OverflowError):
                invalid_time_rows += 1
    submissions.sort(key=lambda pair: pair[0])
    normalized = [(sid, re.sub(r"\s+", "", text)) for sid, text in submissions]
    possible_pairs = len(normalized) * (len(normalized) - 1) // 2
    ratios, near_identical = [], 0
    for index, (_, first) in enumerate(normalized):
        if len(ratios) >= max_similarity_pairs:
            break
        for _, second in normalized[index + 1:]:
            if len(ratios) >= max_similarity_pairs:
                break
            ratio = SequenceMatcher(None, first, second, autojunk=False).ratio()
            ratios.append(ratio)
            near_identical += ratio >= .95
    return {
        "version": 1,
        "counts": {"records": len(records), "data_purpose": dict(sorted(purposes.items())),
                   "collection_method": dict(sorted(methods.items())),
                   "record_kind": dict(sorted(kinds.items())),
                   "batch_id": dict(sorted(batches.items())),
                   "assessment_status": dict(sorted(statuses.items()))},
        "submissions": {
            "count": len(submissions),
            "unique_exact": len({text for _, text in submissions}),
            "exact_duplicate_groups": _duplicate_groups(submissions),
            "whitespace_normalized_duplicate_groups": _duplicate_groups(normalized),
            "length_characters": _distribution([len(text) for _, text in submissions]),
            "similarity": {"method": "difflib.SequenceMatcher(autojunk=False), whitespace removed",
                           "possible_pairs": possible_pairs, "pairs_compared": len(ratios),
                           "truncated": len(ratios) < possible_pairs,
                           "pairs_at_least_0_95": near_identical, **_distribution(ratios)},
        },
        "api_event_evidence": {
            "reply_events": reply_count, "pilot_or_live_reply_events": non_demo_replies,
            "reply_events_with_nonnegative_finite_latency": timed_replies,
            "reply_events_with_reported_model": reported_models, "model_error_events": model_errors,
            "interpretation": "Recorded platform events are API-flow evidence, not independent network verification or evidence of human participation.",
        },
        "session_duration_seconds": {**_distribution(durations), "invalid_intervals": invalid_time_rows},
        "time_semantics": {
            "created_at": "Server timestamp when the questionnaire was submitted and the translation session began.",
            "ended_at": "Server timestamp of the first frozen final-translation submission.",
            "duration": "ended_at minus created_at measures the translation session, not the whole questionnaire or total participation time.",
            "provenance_evidence_time": "Time of source verification or operator attestation; never a replacement participant timestamp.",
        },
        "interpretation": {
            "purpose_and_source": "data_purpose is intended use; collection_method is independently evidenced source.",
            "unverified": "No source attestation is recorded; this does not classify the record as human or automated.",
            "descriptive_only": "Duplicates, lengths, similarity and timing are descriptive checks, not fabricated-data verdicts, quality scores or fairness-effect estimates.",
        },
    }
