from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from impostarr.config import ApiKeyEntry, AuthConfig, Settings
from impostarr.jobs import create_job
from impostarr.main import create_app
from impostarr.models import File, Instance

API_PREFIX = "/api/v1"


def _settings(tmp_path, **auth_kwargs) -> Settings:
    return Settings(state_dir=tmp_path / "state", auth=AuthConfig(**auth_kwargs))


def _make_pending_job(session_factory) -> int:
    with session_factory() as session:
        instance = Instance(name="main", url="http://sonarr.test:8989")
        session.add(instance)
        session.flush()
        file = File(
            instance_id=instance.id,
            sonarr_path="/tv/Show/S01E01.mkv",
            local_path="/media/tv/Show/S01E01.mkv",
            size=1,
            content_hash="hash1",
            series_id=1,
            episode_ids=[1],
            episode_file_id=1,
            quality={},
            languages=[],
        )
        session.add(file)
        session.flush()
        job = create_job(session, file.id)
        return job.id


@pytest.fixture
def app_no_auth(tmp_path):
    return create_app(_settings(tmp_path))


def test_anon_is_admin_on_mutating_route(app_no_auth):
    with TestClient(app_no_auth) as client:
        job_id = _make_pending_job(app_no_auth.state.session_factory)
        response = client.post(f"{API_PREFIX}/jobs/{job_id}/park")
    assert response.status_code == 200
    assert response.json() == {"result": "hold"}


def test_trusted_header_identity_attributed(tmp_path, caplog):
    app = create_app(_settings(tmp_path, trusted_header="X-Authentik-Username"))
    with caplog.at_level(logging.INFO, logger="impostarr.api.auth"), TestClient(app) as client:
        job_id = _make_pending_job(app.state.session_factory)
        response = client.post(
            f"{API_PREFIX}/jobs/{job_id}/park", headers={"X-Authentik-Username": "alice"}
        )
    assert response.status_code == 200
    assert any("identity=alice" in record.message for record in caplog.records)


def test_api_key_identity_attributed(tmp_path, caplog):
    app = create_app(_settings(tmp_path, api_keys=[ApiKeyEntry(name="bob", key="secret-key")]))
    with caplog.at_level(logging.INFO, logger="impostarr.api.auth"), TestClient(app) as client:
        job_id = _make_pending_job(app.state.session_factory)
        response = client.post(
            f"{API_PREFIX}/jobs/{job_id}/park", headers={"X-Api-Key": "secret-key"}
        )
    assert response.status_code == 200
    assert any("identity=bob" in record.message for record in caplog.records)


def test_unknown_api_key_falls_back_to_anon(tmp_path, caplog):
    app = create_app(_settings(tmp_path, api_keys=[ApiKeyEntry(name="bob", key="secret-key")]))
    with caplog.at_level(logging.INFO, logger="impostarr.api.auth"), TestClient(app) as client:
        job_id = _make_pending_job(app.state.session_factory)
        response = client.post(
            f"{API_PREFIX}/jobs/{job_id}/park", headers={"X-Api-Key": "wrong-key"}
        )
    assert response.status_code == 200
    assert any("identity=anon" in record.message for record in caplog.records)


def test_group_gate_403_when_group_missing(tmp_path):
    app = create_app(
        _settings(tmp_path, group_header="X-Authentik-Groups", required_group="impostarr-admins")
    )
    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/status")
    assert response.status_code == 403


def test_group_gate_passes_when_group_present(tmp_path):
    app = create_app(
        _settings(tmp_path, group_header="X-Authentik-Groups", required_group="impostarr-admins")
    )
    with TestClient(app) as client:
        response = client.get(
            f"{API_PREFIX}/status", headers={"X-Authentik-Groups": "other,impostarr-admins"}
        )
    assert response.status_code == 200


def test_healthz_exempt_from_group_gate(tmp_path):
    app = create_app(
        _settings(tmp_path, group_header="X-Authentik-Groups", required_group="impostarr-admins")
    )
    with TestClient(app) as client:
        response = client.get(f"{API_PREFIX}/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
