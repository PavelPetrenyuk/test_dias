from __future__ import annotations

import importlib
import json
from pathlib import Path

from fastapi.testclient import TestClient


def test_api_and_worker_process_one_job(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobs.db'}")
    monkeypatch.setenv("API_TOKEN", "test-token")
    monkeypatch.setenv("DATA_DIRECTORY", str(tmp_path / "data"))

    import article_masks.api as api
    import article_masks.database as database
    import article_masks.worker as worker

    database._engine = None
    database._session_factory = None
    api = importlib.reload(api)

    headers = {"Authorization": "Bearer test-token"}
    csv_content = b'manufacturer;part_group;masks\nMaker;Group;"A*, B-*"\n'

    with TestClient(api.app) as client:
        response = client.post(
            "/v1/jobs",
            headers=headers,
            files={"file": ("input.csv", csv_content, "text/csv")},
            data={"shard_size": "20", "buffer_records": "7"},
        )
        assert response.status_code == 202
        job = response.json()
        assert job["status"] == "queued"

        job_id = worker._claim_next_job()
        assert job_id is not None
        assert str(job_id) == job["id"]
        worker._process_job(job_id)

        status_response = client.get(job["status_url"], headers=headers)
        assert status_response.status_code == 200
        completed_job = status_response.json()
        assert completed_job["status"] == "completed"
        assert completed_job["generated_articles"] == 72

        manifest_response = client.get(
            completed_job["manifest_url"],
            headers=headers,
        )
        assert manifest_response.status_code == 200
        manifest = json.loads(manifest_response.content)
        assert manifest["generated_articles"] == 72

        first_file = manifest["groups"][0]["masks"][0]["shards"][0]["path"]
        file_response = client.get(
            f"/v1/jobs/{job['id']}/files/{first_file}",
            headers=headers,
        )
        assert file_response.status_code == 200
        assert file_response.content.startswith(b"\x1f\x8b")


def test_api_requires_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'jobs.db'}")
    monkeypatch.setenv("API_TOKEN", "test-token")
    monkeypatch.setenv("DATA_DIRECTORY", str(tmp_path / "data"))

    import article_masks.api as api
    import article_masks.database as database

    database._engine = None
    database._session_factory = None
    api = importlib.reload(api)

    with TestClient(api.app) as client:
        response = client.get("/v1/jobs")
    assert response.status_code == 401
