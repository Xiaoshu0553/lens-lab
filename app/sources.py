"""Read-only source library derived from a task's frozen reference text."""

from __future__ import annotations

import re


SOURCE_LINE = re.compile(r"^([A-Z][0-9]*)｜(.+)$")


def documents(reference_text: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for raw in reference_text.splitlines():
        line = raw.strip()
        match = SOURCE_LINE.match(line)
        if match:
            source_id, body = match.groups()
            title = body.split("，", 1)[0].split("。", 1)[0]
            found.append({"id": source_id, "title": title, "body": line})
        elif line and found:
            found[-1]["body"] += "\n" + line
    return found or [{"id": "SOURCE", "title": "统一参考资料", "body": reference_text}]


def search(source_docs: list[dict[str, str]], query: str) -> list[dict]:
    q = "".join(query.lower().split())
    fragments = {q[i:i + 2] for i in range(len(q) - 1)} if len(q) > 1 else ({q} if q else set())
    results = []
    for document in source_docs:
        body = "".join(document["body"].lower().split())
        score = (10 if q and q in body else 0) + sum(body.count(fragment) for fragment in fragments)
        if q and not score:
            continue
        results.append({"id": document["id"], "title": document["title"],
                        "snippet": document["body"][:140], "match_score": score})
    return sorted(results, key=lambda item: (-item["match_score"], item["id"]))
