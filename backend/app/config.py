"""Environment-backed tracker configuration."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigurationError(RuntimeError):
    """Raised when required application configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """PostgreSQL connection and asyncpg pool settings."""

    host: str
    port: int
    database: str
    user: str
    password: str
    min_pool_size: int = 1
    max_pool_size: int = 5
    connect_timeout: float = 10.0
    command_timeout: float = 30.0


@dataclass(frozen=True, slots=True)
class TCPSettings:
    """Tracker TCP listener settings."""

    host: str = "0.0.0.0"
    port: int = 8686
    read_timeout_seconds: float = 600.0


@dataclass(frozen=True, slots=True)
class OnlineStatusSettings:
    """Age thresholds used to classify device activity."""

    online_minutes: int = 20
    warning_minutes: int = 60


@dataclass(frozen=True, slots=True)
class AuthSettings:
    """Single-administrator login and signed-cookie settings."""

    admin_username: str
    admin_password_hash: str
    session_secret: str
    session_max_age_seconds: int = 28_800
    cookie_secure: bool = False
    cookie_name: str = "tracker_session"


def load_database_settings(env_file: str | Path | None = None) -> DatabaseSettings:
    """Load database settings from ``.env`` and the process environment."""
    _load_environment(env_file)
    settings = DatabaseSettings(
        host=_required("DB_HOST"),
        port=_as_int("DB_PORT", 5432),
        database=_required("DB_NAME"),
        user=_required("DB_USER"),
        password=_required("DB_PASSWORD"),
        min_pool_size=_as_int("DB_POOL_MIN_SIZE", 1),
        max_pool_size=_as_int("DB_POOL_MAX_SIZE", 5),
        connect_timeout=_as_float("DB_CONNECT_TIMEOUT", 10.0),
        command_timeout=_as_float("DB_COMMAND_TIMEOUT", 30.0),
    )
    if settings.port <= 0 or settings.port > 65535:
        raise ConfigurationError("DB_PORT must be between 1 and 65535")
    if settings.min_pool_size < 1:
        raise ConfigurationError("DB_POOL_MIN_SIZE must be at least 1")
    if settings.max_pool_size < settings.min_pool_size:
        raise ConfigurationError(
            "DB_POOL_MAX_SIZE must be greater than or equal to DB_POOL_MIN_SIZE"
        )
    if settings.connect_timeout <= 0:
        raise ConfigurationError("DB_CONNECT_TIMEOUT must be positive")
    if settings.command_timeout <= 0:
        raise ConfigurationError("DB_COMMAND_TIMEOUT must be positive")
    return settings


def load_tcp_settings(env_file: str | Path | None = None) -> TCPSettings:
    """Load TCP listener settings from ``.env`` and the process environment."""
    _load_environment(env_file)
    settings = TCPSettings(
        host=os.getenv("TCP_HOST", "0.0.0.0").strip(),
        port=_as_int("TCP_PORT", 8686),
        read_timeout_seconds=_as_float("TCP_READ_TIMEOUT_SECONDS", 600.0),
    )
    if not settings.host:
        raise ConfigurationError("TCP_HOST must not be empty")
    if settings.port <= 0 or settings.port > 65535:
        raise ConfigurationError("TCP_PORT must be between 1 and 65535")
    if settings.read_timeout_seconds <= 0:
        raise ConfigurationError("TCP_READ_TIMEOUT_SECONDS must be positive")
    return settings


def load_online_status_settings(
    env_file: str | Path | None = None,
) -> OnlineStatusSettings:
    """Load device activity thresholds from the environment."""
    _load_environment(env_file)
    settings = OnlineStatusSettings(
        online_minutes=_as_int("ONLINE_STATUS_ONLINE_MINUTES", 20),
        warning_minutes=_as_int("ONLINE_STATUS_WARNING_MINUTES", 60),
    )
    if settings.online_minutes < 0:
        raise ConfigurationError("ONLINE_STATUS_ONLINE_MINUTES must not be negative")
    if settings.warning_minutes < settings.online_minutes:
        raise ConfigurationError(
            "ONLINE_STATUS_WARNING_MINUTES must be greater than or equal to "
            "ONLINE_STATUS_ONLINE_MINUTES"
        )
    return settings


def load_auth_settings(env_file: str | Path | None = None) -> AuthSettings:
    """Load administrator credentials and signed-session settings."""
    _load_environment(env_file)
    settings = AuthSettings(
        admin_username=_required("ADMIN_USERNAME"),
        admin_password_hash=_required("ADMIN_PASSWORD_HASH"),
        session_secret=_required("SESSION_SECRET"),
        session_max_age_seconds=_as_int("AUTH_SESSION_MAX_AGE_SECONDS", 28_800),
        cookie_secure=_as_bool("AUTH_COOKIE_SECURE", False),
    )
    if len(settings.admin_username) > 64 or any(
        character.isspace() for character in settings.admin_username
    ):
        raise ConfigurationError(
            "ADMIN_USERNAME must contain 1 to 64 non-whitespace characters"
        )
    if not settings.admin_password_hash.startswith("$argon2"):
        raise ConfigurationError(
            "ADMIN_PASSWORD_HASH must be generated with scripts/generate_admin_credentials.py"
        )
    if len(settings.session_secret) < 32:
        raise ConfigurationError("SESSION_SECRET must contain at least 32 characters")
    if not 300 <= settings.session_max_age_seconds <= 2_592_000:
        raise ConfigurationError(
            "AUTH_SESSION_MAX_AGE_SECONDS must be between 300 and 2592000"
        )
    return settings


def _load_environment(env_file: str | Path | None) -> None:
    dotenv_path = Path(env_file) if env_file is not None else PROJECT_ROOT / ".env"
    load_dotenv(dotenv_path=dotenv_path, override=False)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"required environment variable {name} is not set")
    return value


def _as_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _as_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc


def _as_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean")
