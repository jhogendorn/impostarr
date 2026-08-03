"""Configuration models and loading for Impostarr.

Single YAML file (default `/config/impostarr.yml`, overridable via the
`path` argument to `load_settings` or the `IMPOSTARR_CONFIG` env var), with
env overrides `IMPOSTARR__SECTION__KEY` (scalars) or JSON for list/object
values. Env always wins over file values.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

DEFAULT_CONFIG_PATH = Path("/config/impostarr.yml")


class PathMapping(BaseModel):
    sonarr: str
    local: str


class SonarrInstance(BaseModel):
    name: str
    url: str
    api_key: str
    path_mappings: list[PathMapping] = Field(default_factory=list)
    staging_dir: str
    watch_dirs: list[str] = Field(default_factory=list)  # empty = all
    poll_interval_s: int = 300
    auto_remap: bool = False
    auto_replace: bool = False


class Thresholds(BaseModel):
    quarantine: float = 0.8
    auto: float = 0.4
    alt: float = 0.8
    alt_margin: float = 0.2
    auto_min_evidence: int = 2
    phash_store: float = 0.9


class PluginConfig(BaseModel):
    enabled: bool = True
    weight: float = Field(default=1.0, ge=0)
    options: dict = Field(default_factory=dict)


class PluginsConfig(BaseModel):
    sources: list[str] = Field(default_factory=list)
    identifiers: dict[str, PluginConfig] = Field(default_factory=dict)


class RefSubsConfig(BaseModel):
    api_key: str | None = None
    username: str | None = None
    password: str | None = None
    daily_quota: int = 20
    cache_dir: str | None = None
    manual_dir: str | None = None
    # Fallback language preference order (ISO 639-1), tried after whatever
    # language a caller passes to RefSubService.get() (e.g. a transcript's
    # detected language) -- see refsubs.py's module docstring.
    languages: list[str] = Field(default_factory=lambda: ["en"])
    # OpenSubtitles enforces per-key rate limits in the ballpark of 5
    # req/s (tighter still for login, handled separately). Concurrent
    # get() callers used to stampede /download with no pacing at all,
    # producing 429s in production once whisper-subs started gathering
    # several episodes' worth of lookups at once.
    min_request_interval_s: float = 1.1


class ApiKeyEntry(BaseModel):
    name: str
    key: str


class AuthConfig(BaseModel):
    trusted_header: str | None = None
    group_header: str | None = None
    required_group: str | None = None
    api_keys: list[ApiKeyEntry] = Field(default_factory=list)


class DbConfig(BaseModel):
    dsn: str | None = None  # absent -> SQLite


class TrashConfig(BaseModel):
    enabled: bool = True
    dir: Path = Path("/trash")
    retention_days: int = 14


class WorkersConfig(BaseModel):
    pool_size: int = 2
    # Transcriber backend selection: entry-point name in the
    # `impostarr.transcribers` group ("faster-whisper" | "whisper-cpp" |
    # "remote" | "none", or a third-party backend). See
    # `impostarr.plugins.transcribers.load_transcriber`.
    transcriber: str = "faster-whisper"
    # Backend-specific options, keyed by whatever the chosen backend reads
    # (e.g. whisper-cpp: pywhispercpp params like `language`/`use_gpu`;
    # remote: `base_url`/`api_key`/`model`/`timeout_s`).
    transcriber_options: dict = Field(default_factory=dict)
    # whisper_model: model-size name, shared by the faster-whisper and
    # whisper-cpp backends. whisper_device: CPU/CUDA device selector,
    # consumed by the faster-whisper backend only (whisper-cpp's GPU path
    # is a Vulkan source build, toggled via transcriber_options instead).
    # Kept as top-level fields rather than folded into transcriber_options
    # for backward compatibility with existing configs.
    whisper_model: str = "small"
    whisper_device: str = "auto"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="IMPOSTARR__", env_nested_delimiter="__", yaml_file=DEFAULT_CONFIG_PATH
    )

    sonarr: list[SonarrInstance] = Field(default_factory=list)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    refsubs: RefSubsConfig = Field(default_factory=RefSubsConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    db: DbConfig = Field(default_factory=DbConfig)
    workers: WorkersConfig = Field(default_factory=WorkersConfig)
    trash: TrashConfig = Field(default_factory=TrashConfig)

    # Strongly recommended for first runs against a real library: no files
    # are touched, no Sonarr state is changed; every action is logged as
    # "DRY-RUN would ...". Scoping: only Sonarr API mutations and
    # media-library filesystem operations are suppressed — Impostarr's own
    # database and asset extraction (transcripts, framegrabs, phash corpus)
    # still run, since those are impostarr's own artifacts, not the library.
    dry_run: bool = False

    # When true, `scoring.route()` never returns an auto decision: every
    # remediation candidate demotes to quarantine with a proposed action
    # for human approval, regardless of the per-instance auto_remap/
    # auto_replace flags.
    approval_required: bool = False

    state_dir: Path = Path("/config")
    assets_dir: Path = Path("/assets")
    models_dir: Path = Path("/models")

    @model_validator(mode="after")
    def _unique_sonarr_names(self) -> Settings:
        names = [instance.name for instance in self.sonarr]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate sonarr instance names: {names}")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Env must win over the YAML file, so the YAML source sits below
        # env_settings in priority order.
        return (
            init_settings,
            env_settings,
            YamlConfigSettingsSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )


def load_settings(path: Path | None = None) -> Settings:
    """Load Settings from YAML (if present) with env overrides applied.

    Path resolution: `path` arg, else `IMPOSTARR_CONFIG` env var, else
    `/config/impostarr.yml`. A missing file yields defaults (plus any env
    overrides) rather than raising.
    """
    resolved = path if path is not None else Path(os.environ.get("IMPOSTARR_CONFIG", str(DEFAULT_CONFIG_PATH)))

    # settings_customise_sources is a classmethod without access to
    # per-call state, so the resolved yaml path is threaded through via a
    # throwaway subclass's model_config (YamlConfigSettingsSource falls
    # back to settings_cls.model_config["yaml_file"] when not passed
    # explicitly).
    class _SettingsWithYaml(Settings):
        model_config = SettingsConfigDict(**{**Settings.model_config, "yaml_file": resolved})

    return _SettingsWithYaml()
