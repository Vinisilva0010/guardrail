"""Application configuration, loaded from the environment.

Values are validated at startup so that a missing or malformed setting fails
immediately with a clear message, instead of surfacing later inside a collector.
"""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime settings read from the project .env file."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    db_host: str = Field(default="127.0.0.1")
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str
    db_user: str
    db_password: SecretStr
    database_url: PostgresDsn

    log_level: str = Field(default="INFO")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the settings singleton.

    Cached so that the .env file is read once per process.
    """
    return Settings()  # type: ignore[call-arg]


# --- Risk limits -----------------------------------------------------------
# These are constants, not settings. They are deliberately NOT read from the
# environment: changing a risk limit must require a commit and a review, never
# an edit to a local file. See SPEC.md section 8.

MAX_RISK_PCT_PER_TRADE = Decimal("0.015")
MAX_OPEN_RISK_PCT = Decimal("0.04")
MAX_NEW_POSITIONS_PER_MONTH = 6
MAX_LEVERAGE = Decimal("1")
MAX_REVIEW_HORIZON_DAYS = 30
LOSS_STREAK_COOLDOWN_DAYS = 7
LOSS_STREAK_THRESHOLD = 2
