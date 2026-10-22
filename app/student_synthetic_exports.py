"""Private downloads of the current independent student simulation only.

Downloads are generated from the frozen materials and seed, not participant
databases. The historical cross-age batch is deliberately absent from this API.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import Response

from .student_showcase import BATCH_ID, EVIDENCE_POLICY, NOTICE, RECORD_COUNT, get_student_dataset


EXPORT_FILES = {
    "csv": ("大学生126_合成逐人记录.csv", "126条大学生合成记录 CSV", "text/csv; charset=utf-8"),
    "json": ("大学生126_合成逐人记录.json", "126条大学生合成记录 JSON", "application/json"),
    "readme": ("大学生126_来源与使用说明.txt", "合成数据来源与使用说明", "text/plain; charset=utf-8"),
    "materials_json": ("大学生126_冻结题目与评分.json", "冻结题目、任务与评分 JSON", "application/json"),
    "manifest_json": ("大学生126_生成清单.json", "生成参数与校验摘要 JSON", "application/json"),
    "zip": ("大学生126_合成数据完整包.zip", "大学生合成数据完整 ZIP 包", "application/zip"),
}
EXPECTED_COUNTS = {"total": RECORD_COUNT, "completed": RECORD_COUNT, "dropout": 0}
CALIBRATION_NOTE = (
    "本批次未使用旧600人或旧126人作答，不按目标均值校准，不预设澄清组得分更高。"
    "模拟分差只由固定生成规则产生，不能作为干预效果或研究发现。" + EVIDENCE_POLICY
)


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _readme(dataset: dict) -> str:
    manifest = dataset["manifest"]
    return f"""大学生平台独立合成数据：{BATCH_ID}

{NOTICE}
{CALIBRATION_NOTE}

范围：18–25岁，在读专科或本科；年龄分为18–20、21–23、24–25岁。
每个年龄组42条，其中无额外澄清提示词组S与有澄清提示词组A各21条。
126条均为合成完成记录；没有真实招募、实际参与者或模型调用。
合成身份中的在读确认值不能作为真实学生身份验证。

独立来源：全部重新生成，未读取、改写或重新包装旧跨年龄600/126条记录。
先用固定seed从新版30题材料生成答案，并抽样最终参加和办理选项，再独立分配实验组。
同一生成规则用于两组，答题概率与决策抽样概率不读取实验组。
没有按均分、组差或显著性选样，没有重试seed。
两组对话与来源展示均为人工编写的过程示例，不是模型遵从率的实测结果。

V2过程生成假设：{manifest['evidence_policy']}
A组每轮只问一个事实，两个事实补齐后继续作答；S组可以主动提供这两项事实。
AI资料线索次数按实际模板回复中的明确来源卡片与对应evidence_reveal事件复算，
不是按组别直接填入汇总。各年龄区间A组每条目标来源出现21次，S组轮转3条来源，
各来源出现次数均少于21；这些差异是人工设定的过程假设，不是观察到的干预效果。
答案与最终决策的抽样不读取组别，也不因这项过程假设重新调分。

全部记录的is_synthetic=1、record_kind=synthetic、data_origin=synthetic、
platform_analysis_eligible=0。所有日期与作答/对话/决策时长都是模拟值。
actual_model_latency_ms、mean_reply_seconds均为null；没有reply_latency事件。
synthetic_pretest_time_seconds为null；未记录前测起点，不补造前测时长。
CSV中的JSON对象和数组以JSON文本存储，空白模型延迟单元格表示缺失，不是0秒。
过程提醒按同一derive_process_alerts规则从逐题答案、时点及对话复算，
每条保留触发规则、观测值、来源字段和模拟标记，不因分组或低分直接添加提醒。

问卷版本：{manifest['form_version']}
研究设计：{manifest['design_version']}
生成器版本：{manifest['generator_version']}
生成seed：{manifest['generator_seed']}
分组seed：{manifest['arm_seed']}
冻结材料SHA256：{manifest['materials_sha256']}
逐人记录SHA256：{manifest['records_sha256']}
摘要算法：UTF-8 JSON，sort_keys=True，ensure_ascii=False，separators=(',', ':')。

复算：按冻结材料中每题correct与逐人literacy_answers比较，计算30题原始分、
百分制分与10分宽分层；anchor_score仅对anchor=true题相加。
任务得分为该记录scenario_id对应的decision_choice_id与execution_choice_id分数相加。
可行性为两项success同时为真；最优性另要求参加方案goal_met为真。
得分是规则下的决策质量分，不是现实成功概率，也不是同一人的前后提高。

文件：逐人CSV、含人工模板对话的逐人JSON、冻结问卷与四情景任务JSON、
生成清单JSON及本说明。ZIP只是这些相同文件的打包，不是另一批数据。
正式研究应另行招募真实在读大学生；本批数据仅用于界面、导出与统计流程演示。
"""


def student_artifact_bytes() -> dict[str, bytes]:
    """Materialize reproducible bytes; no study DB or arbitrary path is opened."""
    dataset = get_student_dataset()
    records = dataset["records"]
    output = io.StringIO(newline="")
    item_ids = [item["id"] for item in next(iter(dataset["frozen_materials"].values()))["items"]]
    keys = list(records[0])
    fieldnames = keys + [f"answer_{item_id}" for item_id in item_ids]
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in records:
        flat = {key: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if isinstance(value, (dict, list)) else value for key, value in row.items()}
        flat.update({f"answer_{item_id}": row["literacy_answers"][item_id] for item_id in item_ids})
        writer.writerow(flat)
    files = {"csv": output.getvalue().encode("utf-8-sig"), "json": _json_bytes(records),
             "readme": _readme(dataset).encode("utf-8"),
             "materials_json": _json_bytes(dataset["frozen_materials"]),
             "manifest_json": _json_bytes(dataset["manifest"])}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for key, payload in files.items():
            info = zipfile.ZipInfo(EXPORT_FILES[key][0], date_time=(2026, 10, 22, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            zip_file.writestr(info, payload)
    files["zip"] = archive.getvalue()
    return files


def write_student_artifacts(dataset_root: Path) -> dict:
    """Write a new batch directory, refusing to overwrite differing artifacts."""
    directory = Path(dataset_root) / BATCH_ID
    payloads = student_artifact_bytes()
    for key, payload in payloads.items():
        path = directory / EXPORT_FILES[key][0]
        if path.exists() and path.read_bytes() != payload:
            raise ValueError(f"现有大学生合成文件内容不同，不覆盖：{path.name}")
    directory.mkdir(parents=True, exist_ok=True)
    for key, payload in payloads.items():
        path = directory / EXPORT_FILES[key][0]
        if not path.exists():
            with path.open("xb") as handle:
                handle.write(payload)
    return {"batch_id": BATCH_ID, "directory": str(directory.resolve()),
            "record_count": RECORD_COUNT, "files": [EXPORT_FILES[key][0] for key in payloads]}


def register_student_synthetic_exports(app: FastAPI, require_admin: Callable,
                                       dataset_root: Path | None = None) -> None:
    """Register the current private catalogue, replacing the legacy registrar.

    ``dataset_root`` remains accepted for caller compatibility. Current exports
    always use the code-frozen generator; external or legacy files are not read.
    """
    @app.get("/api/admin/synthetic-datasets")
    def synthetic_datasets(admin: str = Depends(require_admin)):
        manifest = get_student_dataset()["manifest"]
        return {"datasets": [{
            "id": BATCH_ID, "source_batch_id": BATCH_ID, "design_version": manifest["design_version"],
            "label": "126条大学生合成数据（V2过程假设）", "is_synthetic": True,
            "data_origin": "synthetic", "record_kind": "synthetic",
            "platform_database_imported": None, "platform_database_inspected": False,
            "available": True, "verification_status": "deterministic_student_batch_frozen_materials",
            "expected_counts": dict(EXPECTED_COUNTS), "n_total": RECORD_COUNT,
            "n_completed": RECORD_COUNT, "n_dropout": 0,
            "age_range": [18, 25], "age_bands": list(manifest["age_counts"]),
            "education_stages": ["college", "bachelor"], "student_confirmed": True,
            "notice": NOTICE, "calibration_note": CALIBRATION_NOTE,
            "evidence_policy": EVIDENCE_POLICY, "evidence_policy_is_simulated_assumption": True,
            "files": [{"key": key, "label": label, "filename": filename,
                       "available": True, "url": f"/api/admin/synthetic-export?file={key}"}
                      for key, (filename, label, _media) in EXPORT_FILES.items()],
        }]}

    @app.get("/api/admin/synthetic-export")
    def synthetic_export(file: str = Query(...), admin: str = Depends(require_admin)):
        if file not in EXPORT_FILES:
            raise HTTPException(422, "file只能是csv/json/readme/materials_json/manifest_json/zip")
        filename, _label, media = EXPORT_FILES[file]
        return Response(student_artifact_bytes()[file], media_type=media,
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Content-Disposition": f"attachment; filename=student_126_{file}; filename*=UTF-8''{quote(filename)}"})
