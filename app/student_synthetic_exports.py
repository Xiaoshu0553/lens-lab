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
    "本批次未使用旧跨年龄600人或126人作答；V4保留V2大学生合成作答及V3对话，补充回复处理用时估计。"
    "不按目标均值校准，不预设澄清组得分更高。"
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

独立来源：未读取、改写或重新包装旧跨年龄600/126条记录。
V4沿用V2大学生合成批次{manifest['parent_source_batch_id']}中的逐人作答、决策、
人口属性、情景、分组与起止时点，并完整保留V3批次{manifest['prior_process_batch_id']}的
对话正文、动作、时点、资料路径和计数，仅补充逐轮回复处理用时估计。
V2和V3来源本身均不是实际受试者或provider实测数据。
基础作答使用原V2个人随机键复算：先从新版30题材料生成答案和最终选项，再独立分配实验组。
同一生成规则用于两组，答题概率与决策抽样概率不读取实验组。
没有按均分、组差或显著性选样，没有重试seed。
两组对话与来源展示均为人工编写的过程示例，不是模型遵从率的实测结果。

保留的V3过程生成假设：{manifest['evidence_policy']}
独立对话seed：{manifest['dialogue_seed']}，逐人的原V2编号作随机键，首次抽样后不重试。
起始可能未给事实、只给既往名额、只给每周可到场次数，或者已给全两项事实。
A组只澄清缺少的事实，每轮最多一项，补齐即继续；已知条件不重复追问。
S组不主动追问，未知事实仅给条件规则，由用户在开头或后续主动补充。
来源选择按年龄兴趣权重固定抽样，每条同卡最多两次、间隔复核，不要求两组每人次数相等。
轮次、开头、问法、事实补充路径和复核顺序由固定独立随机流生成，全部不超过12轮。
AI资料线索次数按实际模板回复中的明确来源卡片与对应evidence_reveal事件复算，
不是按组别直接填入汇总；重复展示按次数计，不同来源卡数另行去重。
当前A组线索共{manifest['evidence_reveal_totals']['A']}次，S组共{manifest['evidence_reveal_totals']['S']}次，
各年龄及各来源次数见生成清单；这些差异是人工设定的过程假设，不是观察到的干预效果。
答案与最终决策的抽样不读取组别，也不因这项过程假设重新调分。

父批次个人字段指纹：{manifest['parent_profiles_sha256']}
指纹字段：{', '.join(manifest['parent_profile_fields'])}。
逐条parent_record_id指向原V2编号，parent_profile_sha256核对相应字段；
新批次只可导入全新独立数据库，不能覆盖旧批次或实际参与者记录。

全部记录的is_synthetic=1、record_kind=synthetic、data_origin=synthetic、
platform_analysis_eligible=0。所有日期与作答/对话/决策时长都是模拟值。
actual_model_latency_ms仍为null；没有reply_latency实测事件，实际模型调用为0。
AI回复用时来源reply_time_source=estimated，估算器{manifest['reply_time_estimator_version']}，
估计seed为{manifest['reply_time_estimation_seed']}。{manifest['reply_time_estimation_rule']}
每条assistant消息对应一项reply_time_estimates原始明细及reply_time_estimate事件，
保存estimated_latency_ms、is_estimate=true、source=estimated、estimator_version、消息序号、
回复字数与动作、原用户/AI消息时点及可用窗口，不更改既有消息时间戳。
estimated_reply_ms为逐轮毫秒估计的算术均值，mean_reply_seconds为同均值折算秒并保留两位。
逐轮估计共{manifest['reply_time_estimate_count']}项，范围{manifest['estimated_reply_range_ms']}毫秒，
全体逐轮加权均值{manifest['estimated_reply_mean_seconds']}秒；逐记录平均值范围
{manifest['record_mean_reply_seconds_range']}秒。这些值不能当作provider实测性能。
synthetic_pretest_time_seconds为null；未记录前测起点，不补造前测时长。
CSV中的JSON对象和数组以JSON文本存储，空白actual_model_latency_ms表示实测缺失，不是0秒。
过程提醒按同一derive_process_alerts规则从逐题答案、时点及对话复算，
每条保留触发规则、观测值、来源字段和模拟标记，不因分组或低分直接添加提醒。

问卷版本：{manifest['form_version']}
研究设计：{manifest['design_version']}
生成器版本：{manifest['generator_version']}
生成seed：{manifest['generator_seed']}
分组seed：{manifest['arm_seed']}
V3原对话内容指纹：{manifest['prior_process_content_sha256']}
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
            "label": "126条大学生合成数据（V4回复用时估计）", "is_synthetic": True,
            "parent_source_batch_id": manifest["parent_source_batch_id"],
            "parent_profiles_sha256": manifest["parent_profiles_sha256"],
            "prior_process_batch_id": manifest["prior_process_batch_id"],
            "reply_time_source": manifest["reply_time_source"],
            "reply_time_estimator_version": manifest["reply_time_estimator_version"],
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
