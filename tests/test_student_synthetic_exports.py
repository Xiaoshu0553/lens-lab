"""Current student downloads are private, reproducible and never old batches."""
import csv
import io
import json
import zipfile
from urllib.parse import unquote

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.student_showcase import BATCH_ID, get_student_dataset
from app.student_synthetic_exports import (EXPORT_FILES, register_student_synthetic_exports,
                                           student_artifact_bytes, write_student_artifacts)


@pytest.fixture
def client():
    app = FastAPI()

    def require_admin(request: Request):
        if request.headers.get("authorization") != "test-auth":
            raise HTTPException(401, "login required")
        return "admin"

    register_student_synthetic_exports(app, require_admin)
    return TestClient(app)


@pytest.mark.parametrize("path", ["/api/admin/synthetic-datasets"] + [f"/api/admin/synthetic-export?file={key}" for key in EXPORT_FILES])
def test_private_downloads_require_admin(client, path):
    assert client.get(path).status_code == 401


def test_catalogue_contains_only_new126_student_batch(client):
    response = client.get("/api/admin/synthetic-datasets", headers={"authorization": "test-auth"})
    assert response.status_code == 200
    datasets = response.json()["datasets"]
    assert len(datasets) == 1
    data = datasets[0]
    assert data["id"] == BATCH_ID and data["n_total"] == data["n_completed"] == 126
    assert BATCH_ID == "student-126-20261022-v2"
    assert data["n_dropout"] == 0 and data["design_version"] == "university-v1"
    assert data["age_range"] == [18, 25] and data["education_stages"] == ["college", "bachelor"]
    assert "不是真实受试者" in data["notice"] and "不能作为" in data["calibration_note"]
    assert data["evidence_policy_is_simulated_assumption"] is True
    assert "人工模拟假设" in data["evidence_policy"]
    assert {item["key"] for item in data["files"]} == set(EXPORT_FILES)
    assert "20261020-600" not in response.text and "calibrated_csv" not in response.text


@pytest.mark.parametrize("key", list(EXPORT_FILES))
def test_downloads_have_reproducible_bytes_and_safe_headers(client, key):
    response = client.get("/api/admin/synthetic-export", params={"file": key}, headers={"authorization": "test-auth"})
    assert response.status_code == 200 and response.content == student_artifact_bytes()[key]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert EXPORT_FILES[key][0] in unquote(response.headers["content-disposition"])
    assert response.headers["content-type"].startswith(EXPORT_FILES[key][2].split(";")[0])


@pytest.mark.parametrize("key", ["../../lens.sqlite3", "/etc/passwd", "calibrated_csv", "xlsx", "unknown"])
def test_legacy_keys_and_arbitrary_paths_are_rejected(client, key):
    assert client.get("/api/admin/synthetic-export", params={"file": key}, headers={"authorization": "test-auth"}).status_code == 422


def test_csv_json_zip_all_hold_the_same_single_batch():
    payloads = student_artifact_bytes()
    assert payloads == student_artifact_bytes()
    csv_rows = list(csv.DictReader(io.StringIO(payloads["csv"].decode("utf-8-sig"))))
    json_rows = json.loads(payloads["json"])
    assert len(csv_rows) == len(json_rows) == 126
    assert [row["id"] for row in csv_rows] == [row["id"] for row in json_rows]
    for flat, row in zip(csv_rows, json_rows):
        assert json.loads(flat["literacy_answers"]) == row["literacy_answers"]
        assert flat["actual_model_latency_ms"] == flat["mean_reply_seconds"] == ""
        assert flat["platform_analysis_eligible"] == "0" and flat["student_confirmed"] == "1"
        assert flat["synthetic_pretest_time_seconds"] == ""
        assert json.loads(flat["process_alert_details"]) == row["process_alert_details"]
        assert json.loads(flat["flags"]) == row["flags"]
        assert row["source_batch_id"] == BATCH_ID and row["id"].startswith("SYNTH-STUDENT-V2-")
    with zipfile.ZipFile(io.BytesIO(payloads["zip"])) as archive:
        assert set(archive.namelist()) == {EXPORT_FILES[key][0] for key in payloads if key != "zip"}
        for key in payloads:
            if key != "zip":
                assert archive.read(EXPORT_FILES[key][0]) == payloads[key]
    assert json.loads(payloads["manifest_json"]) == get_student_dataset()["manifest"]
    assert "人工模拟假设" in payloads["readme"].decode()
    assert "不是真实模型" in payloads["readme"].decode()


def test_a_legacy_external_directory_cannot_replace_the_current_v2_exports(tmp_path):
    old = tmp_path / "student-126-20261022-v1"
    old.mkdir()
    (old / EXPORT_FILES["json"][0]).write_text('[{"id":"old-v1-only"}]')
    app = FastAPI()
    register_student_synthetic_exports(app, lambda: "test-admin", tmp_path)
    client = TestClient(app)
    rows = client.get("/api/admin/synthetic-export?file=json").json()
    assert len(rows) == 126
    assert all(row["source_batch_id"] == BATCH_ID and row["id"].startswith("SYNTH-STUDENT-V2-") for row in rows)
    assert "old-v1-only" not in json.dumps(rows)


def test_artifact_writer_is_idempotent_and_refuses_changed_files(tmp_path):
    result = write_student_artifacts(tmp_path)
    assert result == write_student_artifacts(tmp_path)
    directory = tmp_path / BATCH_ID
    path = directory / EXPORT_FILES["json"][0]
    assert json.loads(path.read_text()) == get_student_dataset()["records"]
    path.write_text("edited existing archive")
    with pytest.raises(ValueError, match="不覆盖"):
        write_student_artifacts(tmp_path)
    assert path.read_text() == "edited existing archive"
