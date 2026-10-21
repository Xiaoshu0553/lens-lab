"""Descriptive pilot item statistics, never evidence of validation by themselves.

All calculations use complete, valid response vectors from one age-specific form
version. The caller must select the age, version and a prespecified data-quality
cohort; this module does not filter on later experimental performance.
"""

from __future__ import annotations

import math
from collections import Counter

# Operational review settings for this candidate instrument, not universal
# psychometric cutoffs or criteria that establish validity.
EXPLORATORY_N = 30
DESCRIPTIVE_N = 100
TAIL_FRACTION = 0.27
MIN_TAIL_N = 20
LOW_DISTRACTOR_RATE = 0.05


def _correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    xx = sum((x - mx) ** 2 for x in xs)
    yy = sum((y - my) ** 2 for y in ys)
    if xx == 0 or yy == 0:
        return None
    value = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(xx * yy)
    return round(max(-1.0, min(1.0, value)), 3)


def _wilson_interval(hits: int, n: int) -> list[float] | None:
    if not n:
        return None
    z = 1.959963984540054
    p = hits / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return [round(max(0.0, center - half), 3), round(min(1.0, center + half), 3)]


def _tail_groups(rest_scores: list[int]) -> dict:
    """Use item-rest scores and include every tied boundary score.

    Ties are never broken using response order, item correctness, or participant
    IDs. If the two boundary scores coincide, there is no nonoverlapping high/
    low comparison. A statistic based on arbitrarily split ties would be false
    precision, especially in small samples or short forms.
    """
    n = len(rest_scores)
    if n < 4:
        return {"low": [], "high": [], "reason": "insufficient_responses", "target_n": 0}
    target = max(1, math.ceil(n * TAIL_FRACTION))
    ordered = sorted(rest_scores)
    low_cut, high_cut = ordered[target - 1], ordered[-target]
    if low_cut >= high_cut:
        return {"low": [], "high": [], "reason": "overlapping_score_ties", "target_n": target}
    return {"low": [i for i, score in enumerate(rest_scores) if score <= low_cut],
            "high": [i for i, score in enumerate(rest_scores) if score >= high_cut],
            "reason": None, "target_n": target}


def _internal_consistency(binary: list[list[int]], k: int) -> dict:
    n = len(binary)
    totals = [sum(row) for row in binary]
    mean_total = sum(totals) / n if n else 0.0
    variance = sum((score - mean_total) ** 2 for score in totals) / n if n else 0.0
    item_variances = []
    for j in range(k):
        p = sum(row[j] for row in binary) / n if n else 0.0
        item_variances.append(p * (1 - p))
    reason = ("insufficient_responses" if n < 2 else "fewer_than_two_items" if k < 2
              else "zero_total_variance" if variance == 0 else None)
    value = round(k / (k - 1) * (1 - sum(item_variances) / variance), 3) if reason is None else None
    return {"method": "KR-20 / binary Cronbach alpha", "value": value,
            "computable": reason is None, "not_computable_reason": reason,
            "sample_status": "limited" if n < DESCRIPTIVE_N else "descriptive_only",
            "negative_estimate": value is not None and value < 0,
            "constant_item_count": sum(v == 0 for v in item_variances),
            "note": "二元等权计分时 KR-20 与 Cronbach α 相同。内部一致性不能证明单维性、内容效度或跨年龄可比性；多维题卷应结合结构与维度解释，不能为提高系数机械删题。"}


def _validate_items(items: list[dict]) -> None:
    if not items:
        raise ValueError("题卷不能为空")
    ids = [item.get("id") for item in items]
    if any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids):
        raise ValueError("题号必须为不重复的非空字符串")
    for item in items:
        options, correct = item.get("options"), item.get("correct")
        if not isinstance(options, (list, tuple)) or len(options) < 2:
            raise ValueError("每题至少需要两个选项")
        if type(correct) is not int or not 0 <= correct < len(options):
            raise ValueError("题目答案键无效")


def analyze(items: list[dict], responses: list[dict[str, int]], version: str) -> dict:
    _validate_items(items)
    k = len(items)
    required_ids = {item["id"] for item in items}
    valid = []
    excluded = Counter()
    for response in responses:
        if not isinstance(response, dict) or set(response) != required_ids:
            excluded["incomplete_or_different_form"] += 1
        elif any(type(response[item["id"]]) is not int or
                 not 0 <= response[item["id"]] < len(item["options"]) for item in items):
            excluded["invalid_option"] += 1
        else:
            valid.append(response)
    n = len(valid)
    binary = [[int(response[item["id"]] == item["correct"]) for item in items] for response in valid]
    totals = [sum(row) for row in binary]
    reliability = _internal_consistency(binary, k)
    sample_status = ("no_data" if not n else "insufficient" if n < EXPLORATORY_N
                     else "exploratory" if n < DESCRIPTIVE_N else "descriptive_only")
    sample_note = ("尚无本年龄、本版本的合格完整作答；区分度未验证。" if not n else
                   "样本不足 30 份，比例和相关仅供排查，不能认定题目有区分度。" if n < EXPLORATORY_N else
                   "样本不足 100 份，逐题指标为探索性结果，尚不能确认稳定区分度。" if n < DESCRIPTIVE_N else
                   "仅提供本年龄、本版本的描述性指标；仍需内容、作答过程与独立样本证据，不能据此宣布问卷已验证。")
    item_results = []
    for j, item in enumerate(items):
        correct = [row[j] for row in binary]
        rest = [total - hit for total, hit in zip(totals, correct)]
        tails = _tail_groups(rest)
        low, high = tails["low"], tails["high"]
        p_low = sum(correct[i] for i in low) / len(low) if low else None
        p_high = sum(correct[i] for i in high) / len(high) if high else None
        di = round(p_high - p_low, 3) if low and high else None
        unstable_di = n < DESCRIPTIVE_N or min(len(low), len(high)) < MIN_TAIL_N
        uncertain_index = next((index for index, option in enumerate(item["options"])
                                if option.strip() in {"不确定", "不知道", "无法确定", "我暂时不会选"}), None)
        counts = Counter(response[item["id"]] for response in valid)
        uncertain_count = counts[uncertain_index] if uncertain_index is not None else 0
        p = sum(correct) / n if n else None
        r = _correlation(correct, rest)
        flags = []
        if n < DESCRIPTIVE_N:
            flags.append("sample_limited")
        if n and len(set(correct)) == 1:
            flags.append("constant_item")
        if n >= EXPLORATORY_N and p is not None and (p < 0.2 or p > 0.9):
            flags.append("extreme_pass_rate_review")
        if n >= EXPLORATORY_N and r is not None and r < 0.2:
            flags.append("negative_item_rest_review" if r < 0 else "low_item_rest_review")
        if n >= EXPLORATORY_N and di is not None and di < 0:
            flags.append("negative_discrimination_review")
        option_results = []
        for index, option in enumerate(item["options"]):
            role = "correct" if index == item["correct"] else "uncertain" if index == uncertain_index else "distractor"
            rate = counts[index] / n if n else None
            low_selection = (rate < LOW_DISTRACTOR_RATE if role == "distractor" and n >= DESCRIPTIVE_N
                             else None)
            option_results.append({"index": index, "text": option, "role": role,
                                   "count": counts[index], "selection_rate": round(rate, 3) if rate is not None else None,
                                   "nonfunctioning_candidate": low_selection,
                                   "selection_item_rest_r": _correlation([int(response[item["id"]] == index) for response in valid], rest)})
        if any(option["nonfunctioning_candidate"] for option in option_results):
            flags.append("low_selection_distractor_review")
        reason = tails["reason"]
        di_note = ("少于 4 份，无法形成稳定的高低组。" if reason == "insufficient_responses" else
                   "分界同分导致高低组重叠，未强行拆分同分者。" if reason == "overlapping_score_ties" else
                   "以本题以外的总分取上下 27%，分界同分全部纳入；样本或尾组人数较少，差值不稳定。" if unstable_di else
                   "以本题以外的总分取上下 27%，分界同分全部纳入；结果仅供人工复核。")
        item_results.append({
            "id": item["id"], "domain": item.get("domain", "未分类"), "question": item.get("question", ""),
            "draft_difficulty": item.get("draft_difficulty", "unrated"),
            "p_correct": round(p, 3) if p is not None else None,
            "p_correct_ci95": _wilson_interval(sum(correct), n),
            "corrected_item_total_r": r,
            "uncertain_count": uncertain_count,
            "uncertain_rate": round(uncertain_count / n, 3) if n else None,
            "high_low_discrimination": di, "high_n": len(high), "low_n": len(low),
            "high_p_correct": round(p_high, 3) if p_high is not None else None,
            "low_p_correct": round(p_low, 3) if p_low is not None else None,
            "discrimination_unstable": unstable_di or reason is not None,
            "discrimination_note": di_note, "discrimination_not_computable_reason": reason,
            "options": option_results, "review_flags": flags,
            "validation_status": "unvalidated",
        })
    return {"version": version, "n": n, "input_count": len(responses),
            "excluded_count": sum(excluded.values()), "excluded_reasons": dict(excluded),
            "item_count": k, "mean_percent": round(sum(totals) * 100 / (n * k), 2) if n else 0.0,
            "kr20": reliability["value"], "reliability": reliability,
            "validation_status": "unvalidated", "sample_status": sample_status,
            "sample_note": sample_note,
            "method_notes": {"item_rest": "相关与高低组分层均使用扣除本题后的总分，避免本题机械抬高指标。",
                             "interval": "答对比例区间为 95% Wilson 区间；未调整多重比较。",
                             "distractors": "仅在样本不少于 100 份时标出选择率低于 5% 的干扰项候选，需结合题意复核；不确定项不按干扰项删除。",
                             "review": "样本量、p、相关与干扰项阈值是本项目排查候选设置，非普适法则；不自动删题，不证明效度，不跨年龄或版本合并。"},
            "items": item_results}
