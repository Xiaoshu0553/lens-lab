"""Private synthetic artifacts stay separate from participant study records."""
import json
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.synthetic_exports import BATCH_ID, EXPORT_FILES, register_synthetic_exports
from test_flow import auth, settings, start_payload


def write_artifacts(root):
    records = [{"id": f"SYNTHETIC-{index:04d}", "is_synthetic": 1,
                "data_origin": "synthetic", "record_kind": "synthetic", "run_mode": "simulation",
                "is_demo": 1, "platform_analysis_eligible": 0,
                "status": "completed" if index < 587 else "dropout"} for index in range(600)]
    for key, (relative, _label, _filename, _media) in EXPORT_FILES.items():
        path = root / BATCH_ID / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json.dumps(records).encode() if key == "calibrated_json"
                         else f"SYNTHETIC fixture {key}".encode())
    return records


@pytest.fixture
def artifact_client(tmp_path):
    root = tmp_path / "synthetic-datasets"
    write_artifacts(root)
    app = create_app(settings(tmp_path))
    # Reuse the real admin authentication closure, while injecting a fixture-only
    # artifact root even when create_app already registers the production routes.
    summary_route = next(route for route in app.routes if route.path == "/api/admin/summary")
    require_admin = summary_route.dependant.dependencies[0].call
    app.router.routes[:] = [route for route in app.router.routes
                            if getattr(route, "path", "") not in {
                                "/api/admin/synthetic-datasets", "/api/admin/synthetic-export"}]
    register_synthetic_exports(app, require_admin, root)
    return TestClient(app), root


def metadata(client):
    response = client.get("/api/admin/synthetic-datasets", headers=auth())
    assert response.status_code == 200
    return response.json()["datasets"][0]


@pytest.mark.parametrize("path", ["/api/admin/synthetic-datasets"] + [
    f"/api/admin/synthetic-export?file={key}" for key in EXPORT_FILES])
def test_anonymous_visitors_cannot_list_or_download_synthetic_artifacts(artifact_client, path):
    client, _root = artifact_client
    response = client.get(path)
    assert response.status_code == 401
    assert "SYNTHETIC fixture" not in response.text


def test_verified_metadata_reports_observed_counts_and_private_download_links(artifact_client):
    client, root = artifact_client
    data = metadata(client)
    assert data["id"] == BATCH_ID and data["available"] is True
    assert (data["n_total"], data["n_completed"], data["n_dropout"]) == (600, 587, 13)
    assert data["is_synthetic"] is True and data["platform_database_imported"] is False
    assert data["verification_status"] == "verified_synthetic_markers_and_counts"
    assert "不是真实受试者" in data["notice"] and "不能作为研究发现" in data["calibration_note"]
    assert {item["key"] for item in data["files"]} == set(EXPORT_FILES)
    assert all(item["available"] and item["url"].startswith("/api/admin/synthetic-export?file=")
               for item in data["files"])
    assert str(root) not in json.dumps(data)


@pytest.mark.parametrize("key", list(EXPORT_FILES))
def test_downloads_return_selected_bytes_with_neutral_attachment_names(artifact_client, key):
    client, root = artifact_client
    relative, _label, filename, media = EXPORT_FILES[key]
    response = client.get(f"/api/admin/synthetic-export?file={key}", headers=auth())
    assert response.status_code == 200
    assert response.content == (root / BATCH_ID / relative).read_bytes()
    assert response.headers["content-type"].startswith(media.split(";")[0])
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert filename in unquote(response.headers["content-disposition"])
    assert "synthetic" not in filename.lower()


@pytest.mark.parametrize("file", ["../lens.sqlite3", "/etc/passwd", "calibrated_csv/../../secret", "unknown"])
def test_export_key_allowlist_rejects_paths_and_unknown_values(artifact_client, file):
    client, root = artifact_client
    response = client.get("/api/admin/synthetic-export", params={"file": file}, headers=auth())
    assert response.status_code == 422
    assert str(root) not in response.text


def test_missing_optional_artifact_is_unavailable_without_inventing_a_download(artifact_client):
    client, root = artifact_client
    (root / BATCH_ID / EXPORT_FILES["xlsx"][0]).unlink()
    data = metadata(client)
    item = next(item for item in data["files"] if item["key"] == "xlsx")
    assert item["available"] is False and item["url"] is None
    assert data["available"] is True
    response = client.get("/api/admin/synthetic-export?file=xlsx", headers=auth())
    assert response.status_code == 404 and "文件尚不可用" in response.json()["detail"]
    assert str(root) not in response.text


@pytest.mark.parametrize("corruption", ["missing", "unmarked", "wrong_counts", "invalid_json"])
def test_unverified_batch_never_claims_availability_or_serves_files(artifact_client, corruption):
    client, root = artifact_client
    path = root / BATCH_ID / EXPORT_FILES["calibrated_json"][0]
    records = json.loads(path.read_text())
    if corruption == "missing":
        path.unlink()
    elif corruption == "invalid_json":
        path.write_text("{broken-json")
    else:
        if corruption == "unmarked":
            records[0]["is_synthetic"] = 0
        else:
            records[0]["status"] = "dropout"
        path.write_text(json.dumps(records))
    data = metadata(client)
    assert data["available"] is False and data["n_total"] is None
    assert all(not item["available"] and item["url"] is None for item in data["files"])
    response = client.get("/api/admin/synthetic-export?file=zip", headers=auth())
    assert response.status_code == 404 and str(root) not in response.text


def test_artifact_symlink_cannot_escape_the_approved_batch(artifact_client, tmp_path):
    client, root = artifact_client
    outside = tmp_path / "private.csv"
    outside.write_text("private material")
    path = root / BATCH_ID / EXPORT_FILES["natural_csv"][0]
    path.unlink()
    path.symlink_to(outside)
    item = next(item for item in metadata(client)["files"] if item["key"] == "natural_csv")
    assert item["available"] is False
    response = client.get("/api/admin/synthetic-export?file=natural_csv", headers=auth())
    assert response.status_code == 404 and "private material" not in response.text


def test_cookie_login_authorizes_downloads_and_logout_revokes_them(artifact_client):
    client, _root = artifact_client
    assert client.post("/api/admin/login", json={
        "username": "admin", "password": "test-password"}).status_code == 200
    assert client.get("/api/admin/synthetic-datasets").status_code == 200
    assert client.get("/api/admin/synthetic-export?file=calibrated_json").status_code == 200
    assert client.post("/api/admin/logout").status_code == 200
    assert client.get("/api/admin/synthetic-export?file=calibrated_json").status_code == 401


def test_synthetic_downloads_leave_existing_raw_and_analysis_exports_unchanged(artifact_client):
    client, _root = artifact_client
    sid = client.post("/api/start", json=start_payload()).json()["session_id"]
    paths = [f"/api/admin/archive-export?kind={kind}&format={format}"
             for kind in ["raw", "analysis"] for format in ["json", "csv"]]
    before = {path: client.get(path, headers=auth()).content for path in paths}
    assert [row["id"] for row in json.loads(before[paths[0]])] == [sid]
    assert json.loads(before[paths[2]]) == []
    assert metadata(client)["n_total"] == 600
    for key in EXPORT_FILES:
        assert client.get(f"/api/admin/synthetic-export?file={key}", headers=auth()).status_code == 200
    after = {path: client.get(path, headers=auth()).content for path in paths}
    assert after == before
