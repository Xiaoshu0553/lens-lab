#!/usr/bin/env python3
"""Export private researcher review files outside the participant static tree."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.student_review import inventory, render_review_html


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path,
                        default=ROOT.parent / "output" / "2026-10-22-大学生材料审查")
    args = parser.parse_args()
    target = args.output_dir.resolve()
    target.mkdir(parents=True, exist_ok=True)
    report = inventory()
    files = {
        target / "大学生问卷与任务审查.json": json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        target / "大学生问卷与任务审查.html": render_review_html(report),
    }
    for path, content in files.items():
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        print(path)
    print(f"方案{report['design_version']}；统一问卷{report['form_count']}份；"
          f"独立题目{report['form_item_count']}；统一任务{report['unique_task_count']}套；"
          f"私事实情景{report['task_scenario_count']}种")


if __name__ == "__main__":
    main()
