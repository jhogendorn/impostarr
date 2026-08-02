import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from impostarr.config import DEFAULT_CONFIG_PATH, PathMapping, Settings, load_settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Ensure no leaked IMPOSTARR_* env vars from the real environment affect tests.
    monkeypatch.delenv("IMPOSTARR_CONFIG", raising=False)
    monkeypatch.delenv("IMPOSTARR__THRESHOLDS__AUTO", raising=False)
    monkeypatch.delenv("IMPOSTARR__SONARR", raising=False)


def test_defaults_applied_when_sections_omitted(tmp_path):
    settings = load_settings(tmp_path / "missing.yml")

    assert settings.sonarr == []
    assert settings.thresholds.quarantine == 0.8
    assert settings.thresholds.auto == 0.4
    assert settings.thresholds.alt == 0.8
    assert settings.thresholds.alt_margin == 0.2
    assert settings.thresholds.auto_min_evidence == 2
    assert settings.thresholds.phash_store == 0.9
    assert settings.workers.pool_size == 2
    assert settings.workers.whisper_model == "small"
    assert settings.workers.whisper_device == "auto"
    assert settings.db.dsn is None
    assert settings.state_dir == Path("/config")
    assert settings.assets_dir == Path("/assets")
    assert settings.models_dir == Path("/models")


def test_missing_file_returns_defaults_only_settings(tmp_path):
    settings = load_settings(tmp_path / "does-not-exist.yml")

    assert isinstance(settings, Settings)
    assert settings.sonarr == []
    assert settings.thresholds.auto == 0.4


def test_yaml_load_round_trip(tmp_path):
    config_path = tmp_path / "impostarr.yml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "sonarr": [
                    {
                        "name": "main",
                        "url": "http://sonarr:8989",
                        "api_key": "abc123",
                        "staging_dir": "/media/staging",
                        "path_mappings": [{"sonarr": "/tv", "local": "/media/tv"}],
                        "watch_dirs": ["/media/tv/Show"],
                        "poll_interval_s": 60,
                        "auto_remap": True,
                    }
                ],
                "thresholds": {"auto": 0.5},
            }
        )
    )

    settings = load_settings(config_path)

    assert len(settings.sonarr) == 1
    inst = settings.sonarr[0]
    assert inst.name == "main"
    assert inst.url == "http://sonarr:8989"
    assert inst.api_key == "abc123"
    assert inst.staging_dir == "/media/staging"
    assert inst.path_mappings == [PathMapping(sonarr="/tv", local="/media/tv")]
    assert inst.watch_dirs == ["/media/tv/Show"]
    assert inst.poll_interval_s == 60
    assert inst.auto_remap is True
    assert inst.auto_replace is False
    assert settings.thresholds.auto == 0.5
    assert settings.thresholds.quarantine == 0.8  # untouched default retained


def test_scalar_env_override_wins_over_file(tmp_path, monkeypatch):
    config_path = tmp_path / "impostarr.yml"
    config_path.write_text(yaml.safe_dump({"thresholds": {"auto": 0.5}}))
    monkeypatch.setenv("IMPOSTARR__THRESHOLDS__AUTO", "0.3")

    settings = load_settings(config_path)

    assert settings.thresholds.auto == 0.3
    assert settings.thresholds.quarantine == 0.8


def test_json_list_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "IMPOSTARR__SONARR",
        json.dumps(
            [
                {
                    "name": "env-instance",
                    "url": "http://sonarr2:8989",
                    "api_key": "xyz",
                    "staging_dir": "/staging2",
                }
            ]
        ),
    )

    settings = load_settings(tmp_path / "missing.yml")

    assert len(settings.sonarr) == 1
    assert settings.sonarr[0].name == "env-instance"
    assert settings.sonarr[0].poll_interval_s == 300


def test_bare_settings_defaults_to_standard_config_path():
    # Direct instantiation (no load_settings) must still read the standard
    # /config/impostarr.yml path rather than silently skipping the file.
    assert Settings.model_config["yaml_file"] == DEFAULT_CONFIG_PATH


def test_duplicate_sonarr_instance_names_rejected():
    with pytest.raises(ValidationError):
        Settings(
            sonarr=[
                {"name": "dup", "url": "http://a", "api_key": "k1", "staging_dir": "/s1"},
                {"name": "dup", "url": "http://b", "api_key": "k2", "staging_dir": "/s2"},
            ]
        )
