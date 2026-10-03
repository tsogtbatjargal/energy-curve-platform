"""Runtime settings, read from the environment (mise loads .env locally)."""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    eia_api_key: SecretStr | None = None
    eia_base_url: str = "https://api.eia.gov/v2"
    data_dir: Path = Path("data")
    log_level: str = "INFO"
