from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field


class AzureOpenAISettings(BaseModel):
    enabled: bool = False
    endpoint: str = ""
    api_key: str = ""
    deployment_name: str = ""
    api_version: str = ""
    temperature: float = 0.1

    @property
    def is_configured(self) -> bool:
        return bool(self.endpoint and self.api_key and self.deployment_name)


class SupervisorSettings(BaseModel):
    azure_openai: AzureOpenAISettings = Field(default_factory=AzureOpenAISettings)


SETTINGS_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.json"


def _load_json_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _settings_path(config_path: str | None) -> Path:
    return Path(config_path or os.getenv("SUPERVISOR_SETTINGS_PATH", str(SETTINGS_PATH)))


_ENV_TO_SETTING = {
    "AZURE_OPENAI_ENDPOINT": "endpoint",
    "AZURE_OPENAI_API_KEY": "api_key",
    "AZURE_OPENAI_DEPLOYMENT": "deployment_name",
    "AZURE_OPENAI_API_VERSION": "api_version",
}


def load_settings(config_path: str | None = None) -> SupervisorSettings:
    path = _settings_path(config_path)
    raw = _load_json_settings(path)
    azure = dict(raw.get("azure_openai", {}))

    if os.getenv("AZURE_OPENAI_ENABLED") is not None:
        azure["enabled"] = os.getenv("AZURE_OPENAI_ENABLED", "").lower() in {"1", "true", "yes", "on"}
    for env_key, setting_key in _ENV_TO_SETTING.items():
        value = os.getenv(env_key)
        if value:
            azure[setting_key] = value
    if os.getenv("AZURE_OPENAI_TEMPERATURE"):
        azure["temperature"] = float(os.getenv("AZURE_OPENAI_TEMPERATURE", "0.1"))

    return SupervisorSettings(azure_openai=AzureOpenAISettings(**azure))
