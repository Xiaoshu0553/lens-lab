"""Authenticated, read-only downloads of one explicitly synthetic artifact batch.

This module never opens the participant database. File keys and the approved
batch are fixed here; neither endpoint accepts filesystem paths from callers.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse


BATCH_ID = "20261020-600-c11cc0fd1da3"
CONTENT_DIRECTORY = Path("SYNTHETIC_600人数据设计")
EXPECTED_COUNTS = {"total": 600, "completed": 587, "dropout": 13}
EXPORT_FILES = {
    "calibrated_csv": (
        CONTENT_DIRECTORY / "SYNTHETIC_校准演示_逐人记录.csv",
        "校准演示逐人记录 CSV", "600_校准演示_逐人记录.csv", "text/csv; charset=utf-8"),
    "calibrated_json": (
        CONTENT_DIRECTORY / "SYNTHETIC_校准演示_逐人记录.json",
        "校准演示逐人记录 JSON", "600_校准演示_逐人记录.json", "application/json"),
    "natural_csv": (
        CONTENT_DIRECTORY / "SYNTHETIC_自然波动_逐人记录.csv",
        "自然波动逐人记录 CSV", "600_自然波动_逐人记录.csv", "text/csv; charset=utf-8"),
    "pretest_csv": (
        CONTENT_DIRECTORY / "SYNTHETIC_前测30题答案.csv",
        "前测30题答案 CSV", "600_前测30题答案.csv", "text/csv; charset=utf-8"),
    "xlsx": (
        CONTENT_DIRECTORY / "合成数据设计与检验.xlsx",
        "合成数据设计与检验 Excel", "600_数据设计与检验.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "zip": (
        Path("synthetic-600.zip"), "合成数据完整 ZIP 包", "600人_数据设计完整包.zip", "application/zip"),
}

NOTICE = "全部为合成模拟数据，包含600个模拟记录、587个完成记录和13个脱落记录，不是真实受试者数据。"
CALIBRATION_NOTE = (
    "校准演示版的完成记录均值按截图目标设计；其结果是生成条件，不能作为研究发现。"
    "自然波动版也来自同一合成模型。两版均不进入平台原始记录或正式分析导出。"
)


def _approved_path(dataset_root: Path, relative: Path) -> Path | None:
    """Reject missing files and symlinks escaping the configured artifact root."""
    root = dataset_root.resolve()
    candidate = root / BATCH_ID / relative
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root / BATCH_ID) or not resolved.is_file():
            return None
    except (OSError, RuntimeError):
        return None
    return resolved


def _batch_verification(dataset_root: Path) -> tuple[bool, str, dict | None]:
    path = _approved_path(dataset_root, EXPORT_FILES["calibrated_json"][0])
    if path is None:
        return False, "missing", None
    try:
        records = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(records, list) or len(records) != EXPECTED_COUNTS["total"]:
            return False, "invalid_synthetic_records", None
        for row in records:
            if (not isinstance(row, dict) or row.get("is_synthetic") != 1
                    or row.get("data_origin") != "synthetic"
                    or row.get("record_kind") != "synthetic"
                    or row.get("run_mode") != "simulation" or row.get("is_demo") != 1
                    or row.get("platform_analysis_eligible") != 0
                    or row.get("status") not in {"completed", "dropout"}):
                return False, "invalid_synthetic_records", None
        counts = {"total": len(records),
                  "completed": sum(row["status"] == "completed" for row in records),
                  "dropout": sum(row["status"] == "dropout" for row in records)}
        if counts != EXPECTED_COUNTS:
            return False, "unexpected_record_counts", None
    except (OSError, ValueError, TypeError, UnicodeError):
        return False, "invalid_synthetic_records", None
    return True, "verified_synthetic_markers_and_counts", counts


def register_synthetic_exports(app: FastAPI, require_admin: Callable, dataset_root: Path) -> None:
    """Add private artifact endpoints without changing existing study exports."""
    dataset_root = Path(dataset_root)

    @app.get("/api/admin/synthetic-datasets")
    def synthetic_datasets(admin: str = Depends(require_admin)):
        verified, status, counts = _batch_verification(dataset_root)
        files = []
        for key, (relative, label, filename, _media) in EXPORT_FILES.items():
            available = verified and _approved_path(dataset_root, relative) is not None
            files.append({"key": key, "label": label, "filename": filename,
                          "available": available,
                          "url": f"/api/admin/synthetic-export?file={key}" if available else None})
        return {"datasets": [{
            "id": BATCH_ID, "label": "600人合成数据设计（2026-10-20）",
            "is_synthetic": True, "data_origin": "synthetic", "record_kind": "synthetic",
            "platform_database_imported": False, "available": any(item["available"] for item in files),
            "verification_status": status, "expected_counts": dict(EXPECTED_COUNTS),
            "n_total": counts["total"] if counts else None,
            "n_completed": counts["completed"] if counts else None,
            "n_dropout": counts["dropout"] if counts else None,
            "notice": NOTICE, "calibration_note": CALIBRATION_NOTE, "files": files,
        }]}

    @app.get("/api/admin/synthetic-export")
    def synthetic_export(file: str = Query(...), admin: str = Depends(require_admin)):
        if file not in EXPORT_FILES:
            raise HTTPException(422, "file 只能是 calibrated_csv/calibrated_json/natural_csv/pretest_csv/xlsx/zip")
        verified, _status, _counts = _batch_verification(dataset_root)
        if not verified:
            raise HTTPException(404, "该合成数据批次未上传完整或未通过合成标记核验，暂不能下载。")
        relative, _label, filename, media = EXPORT_FILES[file]
        path = _approved_path(dataset_root, relative)
        if path is None:
            raise HTTPException(404, "所选合成数据文件尚不可用，请联系管理员核对上传。")
        return FileResponse(path, filename=filename, media_type=media,
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
