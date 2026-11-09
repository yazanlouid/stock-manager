from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# One .env at the repo root, shared with Docker Compose. Found from this file's
# location so it works whichever folder a command runs from.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """App config. Real environment variables override values from .env (CI uses this)."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        # .env also holds the POSTGRES_* values for Docker Compose.
        extra="ignore",
    )

    database_url: str


@lru_cache
def get_settings() -> Settings:
    return Settings()
