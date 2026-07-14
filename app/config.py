"""Load settings from local.settings.json (Azure Functions–style) or environment."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "local.settings.json"


@dataclass(frozen=True)
class Settings:
    sql_connection_string: str
    openai_api_key: str
    openai_model: str
    sql_server_host: str
    host: str
    port: int


def load_settings() -> Settings:
    values: dict[str, str] = {}
    if SETTINGS_PATH.exists():
        with SETTINGS_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
        values = dict(data.get("Values") or {})

    def get(key: str, default: str = "") -> str:
        return os.environ.get(key) or values.get(key) or default

    return Settings(
        sql_connection_string=get(
            "SqlConnectionString",
            "Driver={ODBC Driver 18 for SQL Server};"
            "Server=VMWinSQLS,1433;Database=Prohance;"
            "Trusted_Connection=yes;TrustServerCertificate=yes;Encrypt=no;",
        ),
        openai_api_key=get("OpenAIApiKey"),
        openai_model=get("OpenAIModel", "gpt-4o-mini"),
        sql_server_host=get("SqlServerHost", "VMWinSQLS"),
        host=get("FLASK_HOST", "127.0.0.1"),
        port=int(get("FLASK_PORT", "5000")),
    )
