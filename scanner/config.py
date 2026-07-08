"""Multi-environment configuration (004).

Source of truth: specs/004-multi-env-uploads/contracts/config-schema.md.

Configuration is loaded from environment variables (with optional ``.env``
support), nested via the ``__`` separator. Validation failures raise
:class:`ConfigError` with a single message naming the offending field.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from .machine import InvalidMachineIdentityError, MachineIdentity

logger = logging.getLogger(__name__)

_VALID_ENV_NAMES = ("production", "staging")
_DEFAULT_BACKEND = {
    "production": "https://adg.mpsinc.io",
    "staging": "https://dev.adg.mpsinc.io",
}


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid; aborts startup."""


class NTPSettings(BaseModel):
    """Where and how often to query the NTP source (data-model.md)."""

    model_config = ConfigDict(frozen=True)

    source: str = "pool.ntp.org"
    check_interval_seconds: int = 3600
    max_drift_seconds: float = 1.0
    correct_clock_command: str | None = "/usr/local/libexec/pms-scanner-correct-clock"
    startup_required: bool = True
    startup_timeout_seconds: int = 30

    @field_validator("correct_clock_command", mode="before")
    @classmethod
    def _blank_command_is_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v


class NotifySettings(BaseModel):
    """SMTP settings for operator email alerts on unrecoverable failures.

    Fully optional: email is only sent when ``is_configured`` is true (a host,
    from-address and support-address are all present). Left unset, the scanner
    still quarantines failed files — it just logs a warning instead of emailing.
    """

    model_config = ConfigDict(frozen=True)

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    use_tls: bool = True
    from_addr: str | None = None
    support_addr: str = "support@mpsinc.io"
    timeout_seconds: int = 30

    @field_validator("smtp_host", "smtp_username", "from_addr", mode="before")
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @property
    def is_configured(self) -> bool:
        """True when enough is set to actually deliver mail."""
        return bool(self.smtp_host and self.from_addr and self.support_addr)


class _EnvBlock(BaseModel):
    """Raw, all-optional view of one ``ENV_<NAME>__*`` block.

    Kept fully optional so the assembly validator can emit precise,
    field-named errors rather than pydantic's generic 'field required'.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    watch_dir: Path | None = None
    backend_base_url: str | None = None
    api_token: SecretStr | None = None
    requisition_id: UUID | None = None
    schedule_offset_seconds: int | None = None

    @field_validator("requisition_id", "backend_base_url", mode="before")
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v


class Environment(BaseModel):
    """A named, validated upload target (data-model.md)."""

    model_config = ConfigDict(frozen=True)

    name: Literal["production", "staging"]
    enabled: bool = True
    watch_dir: Path
    backend_base_url: str
    api_token: SecretStr
    requisition_id: UUID | None = None
    schedule_offset_seconds: int

    @property
    def in_progress_root(self) -> Path:
        return Path(self.watch_dir) / "in-progress"

    def in_progress_dir(self, machine: MachineIdentity) -> Path:
        return Path(self.watch_dir) / "in-progress" / str(machine.name)

    @property
    def processed_dir(self) -> Path:
        return Path(self.watch_dir) / "processed"

    @property
    def failed_dir(self) -> Path:
        """Quarantine for files the backend permanently refused.

        A file moved here is NOT re-scanned (``run_once`` only reads the watch
        dir and this machine's in-progress subfolder), which is what stops the
        claim→reject→return loop for unrecoverable uploads.
        """
        return Path(self.watch_dir) / "failed"


class AppSettings(BaseSettings):
    """Top-level settings: machine identity + NTP + environment list."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    machine_identity: str = ""
    ntp: NTPSettings = Field(default_factory=NTPSettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)

    environments_raw: str = Field(
        default="",
        validation_alias=AliasChoices("environments_raw", "environments"),
    )
    env_production: _EnvBlock | None = None
    env_staging: _EnvBlock | None = None

    # Shared.
    dashboard_port: int = 8080
    file_settle_seconds: float = 10.0
    upload_timeout_seconds: int = 30
    upload_max_retries: int = 3
    upload_retry_max_wait_seconds: int = 10
    log_level: str = "INFO"

    _machine: MachineIdentity = PrivateAttr()
    _environments: list[Environment] = PrivateAttr(default_factory=list)

    @property
    def machine(self) -> MachineIdentity:
        return self._machine

    @property
    def environments(self) -> list[Environment]:
        return self._environments

    @property
    def enabled_environments(self) -> list[Environment]:
        return [e for e in self._environments if e.enabled]

    @model_validator(mode="after")
    def _assemble(self) -> AppSettings:
        try:
            machine = MachineIdentity(self.machine_identity)
        except InvalidMachineIdentityError as exc:
            raise ValueError(str(exc)) from exc

        raw = self.environments_raw.strip()
        if raw:
            names = [n.strip().lower() for n in raw.split(",") if n.strip()]
            unknown = [n for n in names if n not in _VALID_ENV_NAMES]
            if unknown:
                raise ValueError(
                    f"ENVIRONMENTS contains unknown environment(s) {unknown}; "
                    f"valid names are {list(_VALID_ENV_NAMES)}"
                )
            blocks = {n: getattr(self, f"env_{n}") for n in names}
        else:
            raise ValueError(
                "ENVIRONMENTS is required (comma-separated, e.g. "
                "'production,staging')"
            )

        environments: list[Environment] = [
            self._build_env(name, blocks[name]) for name in names
        ]

        self._check_distinct(environments)

        object.__setattr__(self, "_machine", machine)
        object.__setattr__(self, "_environments", environments)
        return self

    def _build_env(self, name: str, block: _EnvBlock | None) -> Environment:
        upper = name.upper()
        if block is None:
            raise ValueError(
                f"ENV_{upper}__* configuration block is missing for "
                f"environment '{name}'"
            )
        if block.watch_dir is None:
            raise ValueError(f"ENV_{upper}__WATCH_DIR is required")
        if block.api_token is None or not block.api_token.get_secret_value().strip():
            raise ValueError(f"ENV_{upper}__API_TOKEN must be non-empty")
        if block.schedule_offset_seconds is None:
            raise ValueError(f"ENV_{upper}__SCHEDULE_OFFSET_SECONDS is required")
        offset = block.schedule_offset_seconds
        if not 0 <= offset <= 59:
            raise ValueError(
                f"ENV_{upper}__SCHEDULE_OFFSET_SECONDS must be in [0, 59], "
                f"got {offset} (environment '{name}')"
            )
        backend = block.backend_base_url or _DEFAULT_BACKEND[name]
        self._check_scheme(upper, backend)
        return Environment(
            name=name,  # type: ignore[arg-type]
            enabled=block.enabled,
            watch_dir=block.watch_dir,
            backend_base_url=backend.rstrip("/"),
            api_token=block.api_token,
            requisition_id=block.requisition_id,
            schedule_offset_seconds=offset,
        )

    def _check_scheme(self, upper: str, url: str) -> None:
        # HTTPS is required unconditionally: uploads carry patient scans,
        # so there is no plaintext-HTTP escape hatch (not even gated on
        # DEBUG) — a misconfigured dev env must never exfiltrate PHI in
        # cleartext.
        if not url.startswith("https://"):
            raise ValueError(
                f"ENV_{upper}__BACKEND_BASE_URL must be https:// (got {url!r})"
            )

    @staticmethod
    def _check_distinct(envs: list[Environment]) -> None:
        enabled = [e for e in envs if e.enabled]
        for i, a in enumerate(enabled):
            for b in enabled[i + 1 :]:
                if a.watch_dir.resolve() == b.watch_dir.resolve():
                    raise ValueError(
                        f"Environments '{a.name}' and '{b.name}' point to the "
                        f"same watch folder {a.watch_dir} — routing would be "
                        f"ambiguous (FR-009)"
                    )
                if a.schedule_offset_seconds == b.schedule_offset_seconds:
                    raise ValueError(
                        f"Environments '{a.name}' and '{b.name}' share schedule "
                        f"offset :{a.schedule_offset_seconds:02d} — coincident "
                        f"polls defeat staggering (FR-006c)"
                    )


def _format(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc}: {msg}" if loc and loc not in msg else msg)
    return " ; ".join(parts) or str(exc)


def load_settings(*, dotenv: bool = True) -> AppSettings:
    """Build and validate :class:`AppSettings`, mapping errors to ConfigError.

    ``dotenv=False`` skips the ``.env`` file (used by hermetic unit tests).
    """
    try:
        if dotenv:
            return AppSettings()
        return AppSettings(_env_file=None)  # type: ignore[call-arg]
    except ValidationError as exc:
        raise ConfigError(_format(exc)) from exc
