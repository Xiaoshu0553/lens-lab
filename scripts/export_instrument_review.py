#!/usr/bin/env python3
"""Export private researcher review files outside the participant static tree."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.instrument_review import inventory, render_review_html


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path,
                        default=ROOT.parent / "output" / "2026-10-19-问卷任务修订")
    args = parser.parse_args()
    target = args.output_dir.resolve()
    target.mkdir(parents=True, exist_ok=True)
    report = inventory()
    files = {
        target / "全部问卷与任务审查.json": json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        target / "全部问卷与任务审查.html": render_review_html(report),
    }
    for path, content in files.items():
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        print(path)
    print(f"旧题{report['legacy_candidate_count']}；新表单题{report['form_item_count']}；任务案例{report['task_scenario_count']}")


if __name__ == "__main__":
    main()
