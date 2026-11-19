"""Typed, validated configuration: config/config.yaml and one YAML file per job in config/jobs/."""

import re
from pathlib import Path
from typing import Any, Optional, Union

import yaml
from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import settings

PathLike = Union[str, Path]

HOST_PATTERN = r"^([A-Za-z0-9][A-Za-z0-9._-]{0,63})?$"
JOB_ID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
# The template ships this placeholder; `job new` and `job adopt` replace it with a real id.
NIL_JOB_ID = "00000000-0000-4000-8000-000000000000"
EMAIL_ADDRESS = re.compile(r"^[^@\s<>]+@[^@\s<>]+$")


class ConfigError(Exception):
    """Raised for a missing or invalid configuration file, with a readable message."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _blank_means_default(cls, data: Any) -> Any:
        # A key left empty in YAML loads as None; treat it as not set.
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if value is not None}
        return data


class Retention(_Model):
    keep_last: int = Field(0, ge=0)
    keep_hourly: int = Field(0, ge=0)
    keep_daily: int = Field(0, ge=0)
    keep_weekly: int = Field(0, ge=0)
    keep_monthly: int = Field(0, ge=0)
    keep_yearly: int = Field(0, ge=0)
    keep_within: str = ""
    group_by: str = ""
    extra_forget_args: list[str] = Field(default_factory=list)


class ResticOptions(_Model):
    limit_upload: int = Field(0, ge=0)     # KiB/s, 0 = unlimited
    limit_download: int = Field(0, ge=0)
    compression: str = ""                  # "auto" | "max" | "off"
    pack_size: int = Field(0, ge=0)        # target MiB
    cache_dir: str = ""
    no_cache: bool = False
    retry_lock: str = ""                   # e.g. "5m"
    one_file_system: bool = False
    exclude_caches: bool = True
    exclude_if_present: list[str] = Field(default_factory=list)
    exclude_larger_than: str = ""
    host: str = ""                         # hostname recorded on restic snapshots
    read_concurrency: int = Field(0, ge=0)
    extra_backup_args: list[str] = Field(default_factory=list)

    @field_validator("compression")
    @classmethod
    def _known_compression(cls, value: str) -> str:
        if value not in ("", "auto", "max", "off"):
            raise ValueError("must be auto, max or off")
        return value


class NotifyEvent(_Model):
    enabled: bool = False


class NotifyOn(_Model):
    error: NotifyEvent = Field(default_factory=lambda: NotifyEvent(enabled=True))
    backup_complete: NotifyEvent = Field(default_factory=NotifyEvent)


class EmailConfig(_Model):
    smtp_server: str = ""
    smtp_port: int = Field(587, ge=1, le=65535)
    use_tls: bool = True
    from_addr: str = ""
    to_addrs: list[str] = Field(default_factory=list)
    notify_on: NotifyOn = Field(default_factory=NotifyOn)

    @field_validator("to_addrs")
    @classmethod
    def _check_recipients(cls, value: list) -> list:
        for item in value:
            if not EMAIL_ADDRESS.match(item):
                raise ValueError(f"'{item}' is not an email address")
        return value

    @field_validator("from_addr")
    @classmethod
    def _check_sender(cls, value: str) -> str:
        if value and not EMAIL_ADDRESS.match(value):
            raise ValueError(f"'{value}' is not an email address")
        return value


class StorageConfig(_Model):
    report: bool = True                           # send storage samples to the dashboard
    volume_interval_minutes: int = Field(15, ge=1)
    repo_size_interval_hours: int = Field(12, ge=1)
    source_size: bool = True                      # size of each job's latest snapshot, after each backup


class DashboardConfig(_Model):
    enabled: bool = True   # false forces standalone mode even when JABS_DASHBOARD_URL is set


class Config(_Model):
    repo_path: str = ""
    hostname: str = Field("", pattern=HOST_PATTERN)  # reported to the dashboard; default is the system hostname
    use_common_exclude: bool = True
    verify_after_backup: bool = True
    retention: Retention = Field(default_factory=Retention)
    restic_options: ResticOptions = Field(default_factory=ResticOptions)
    email: EmailConfig = Field(default_factory=EmailConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)


class Schedule(_Model):
    cron: str
    enabled: bool = False

    @field_validator("cron")
    @classmethod
    def _valid_cron(cls, value: str) -> str:
        if len(value.split()) != 5 or not croniter.is_valid(value):
            raise ValueError("must be a valid 5-field cron expression")
        return value


class JobFile(_Model):
    """One config/jobs/*.yaml; retention and restic_options hold only the keys this job overrides."""

    job_id: Optional[str] = Field(None, pattern=JOB_ID_PATTERN)
    job_name: Optional[str] = Field(None, min_length=1, max_length=200)
    paths: list[str] = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    schedules: list[Schedule] = Field(default_factory=list)
    prune_after_backup: bool = False
    verify_after_backup: Optional[bool] = None
    repo_path: Optional[str] = None
    retention: dict = Field(default_factory=dict)
    restic_options: dict = Field(default_factory=dict)
    exclude: list[str] = Field(default_factory=list)

    @field_validator("job_id")
    @classmethod
    def _real_id(cls, value: Optional[str]) -> Optional[str]:
        if value == NIL_JOB_ID:
            raise ValueError("is the template's placeholder; run `python -m app job adopt` to give the job an id")
        return value

    @field_validator("retention")
    @classmethod
    def _check_retention(cls, value: dict) -> dict:
        Retention.model_validate(value)
        return value

    @field_validator("restic_options")
    @classmethod
    def _check_restic_options(cls, value: dict) -> dict:
        ResticOptions.model_validate(value)
        return value


class Job(_Model):
    """A job with the global settings merged in. `job_id` is its identity; `name` is only a label."""

    job_id: str
    name: str
    path: str
    repo_path: str
    paths: list[str]
    tags: list[str]
    schedules: list[Schedule]
    prune_after_backup: bool
    verify_after_backup: bool
    retention: Retention
    restic_options: ResticOptions
    exclude: list[str]

    def enabled_crons(self) -> list:
        return [s.cron for s in self.schedules if s.enabled]


def _format_errors(path: Path, exc: ValidationError) -> str:
    lines = [f"{path}: invalid configuration"]
    for err in exc.errors():
        where = ".".join(str(part) for part in err["loc"]) or "(top level)"
        lines.append(f"  {where}: {err['msg']}")
    return "\n".join(lines)


def _read_yaml(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot be read: {exc}") from exc


def _read_mapping(path: Path) -> dict:
    data = _read_yaml(path)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def load_config(path: Optional[PathLike] = None) -> Config:
    """Load and validate config/config.yaml."""
    path = Path(path or settings.CONFIG_PATH)
    if not path.exists():
        hint = ("copy config/config.example.yaml to config/config.yaml"
                if not settings.LEGACY_CONFIG_PATH.exists()
                else "found the old config/global.yaml; run ./setup.sh or copy it to config/config.yaml")
        raise ConfigError(f"{path} not found; {hint}")
    try:
        return Config.model_validate(_read_mapping(path))
    except ValidationError as exc:
        raise ConfigError(_format_errors(path, exc)) from exc


def job_files() -> list:
    return sorted(settings.JOBS_DIR.glob("*.yaml")) if settings.JOBS_DIR.is_dir() else []


def _common_excludes() -> list:
    if not settings.COMMON_EXCLUDE_PATH.exists():
        return []
    data = _read_yaml(settings.COMMON_EXCLUDE_PATH)
    if data is None:
        return []
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise ConfigError(f"{settings.COMMON_EXCLUDE_PATH}: must be a list of patterns")
    return data


def _set_keys(overrides: dict) -> dict:
    return {key: value for key, value in overrides.items() if value is not None}


def load_job(path: PathLike, cfg: Config) -> Job:
    """Load one job file and merge the global settings into it."""
    path = Path(path)
    try:
        raw = JobFile.model_validate(_read_mapping(path))
    except ValidationError as exc:
        raise ConfigError(_format_errors(path, exc)) from exc
    if not raw.job_id:
        raise ConfigError(f"{path}: no job_id; run `python -m app job adopt {path.name}` to give the job its id")
    repo_path = raw.repo_path or cfg.repo_path
    if not repo_path:
        raise ConfigError(f"{path}: no repo_path (set it in config.yaml or in the job)")
    exclude = (_common_excludes() if cfg.use_common_exclude else []) + raw.exclude
    return Job(
        job_id=raw.job_id, name=raw.job_name or path.stem, path=str(path), repo_path=repo_path, paths=raw.paths,
        tags=raw.tags or [raw.job_id],
        schedules=raw.schedules, prune_after_backup=raw.prune_after_backup,
        verify_after_backup=cfg.verify_after_backup if raw.verify_after_backup is None else raw.verify_after_backup,
        retention=Retention.model_validate({**cfg.retention.model_dump(), **_set_keys(raw.retention)}),
        restic_options=ResticOptions.model_validate({**cfg.restic_options.model_dump(), **_set_keys(raw.restic_options)}),
        exclude=exclude,
    )


def find_job(ref: str, cfg: Config) -> Job:
    """Resolve a job given as a job id, a file name (config/jobs/NAME.yaml), a job_name, or a path to a YAML file."""
    candidate = Path(ref)
    if ref.endswith((".yaml", ".yml")) and candidate.exists():
        return load_job(candidate, cfg)
    by_file = settings.JOBS_DIR / f"{ref}.yaml"
    if by_file.exists():
        return load_job(by_file, cfg)
    jobs, _ = load_jobs(cfg)
    for job in jobs:
        if ref in (job.job_id, job.name):
            return job
    raise ConfigError(f"job '{ref}' not found (looked for {by_file})")


def load_jobs(cfg: Config) -> tuple:
    """Every job file as (jobs, errors); one broken file never hides the others."""
    jobs, errors = [], []
    for path in job_files():
        try:
            jobs.append(load_job(path, cfg))
        except ConfigError as exc:
            errors.append(str(exc))
    ids = [job.job_id for job in jobs]
    for job_id in sorted({i for i in ids if ids.count(i) > 1}):
        files = ", ".join(Path(job.path).name for job in jobs if job.job_id == job_id)
        errors.append(f"job_id {job_id} is used by more than one file ({files}); a copied job file needs its own id "
                      "(delete its job_id line and run `python -m app job adopt`)")
    names = [job.name for job in jobs]
    for name in sorted({n for n in names if names.count(n) > 1}):
        errors.append(f"job name '{name}' is used by more than one file in {settings.JOBS_DIR}")
    return jobs, errors
