"""Settings loaded from environment / .env."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    database_url: str  # may be empty: the web app lets the user connect from the UI
    model: str
    max_rows: int
    max_retries: int


def load_settings(require_db: bool = True) -> Settings:
    load_dotenv()
    url = os.getenv("DATABASE_URL", "")
    if require_db and not url:
        raise RuntimeError("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
    return Settings(
        database_url=url,
        model=os.getenv("DATAMASTER_MODEL", "claude-sonnet-5-5"),
        max_rows=int(os.getenv("DATAMASTER_MAX_ROWS", "200").split("#")[0]),
        max_retries=int(os.getenv("DATAMASTER_MAX_RETRIES", "2").split("#")[0]),
    )
