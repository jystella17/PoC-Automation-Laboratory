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
    api_version: str = "2024-02-01"
    temperature: float = 0.1

    @property
    def is_configured(self) -> bool:
        return bool(self.endpoint and self.api_key and self.deployment_name)


class SupervisorSettings(BaseModel):
    azure_openai: AzureOpenAISettings = Field(default_factory=AzureOpenAISettings)


DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "config" / "supervisor_settings.json"
LEGACY_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.json"


def _load_json_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_settings_path(config_path: str | None) -> Path:
    if config_path:
        return Path(config_path)

    env_path = os.getenv("SUPERVISOR_SETTINGS_PATH")
    if env_path:
        return Path(env_path)

    if DEFAULT_SETTINGS_PATH.exists():
        return DEFAULT_SETTINGS_PATH

    return LEGACY_SETTINGS_PATH


def _extract_azure_settings(raw: dict) -> dict:
    if "azure_openai" in raw and isinstance(raw["azure_openai"], dict):
        return dict(raw["azure_openai"])

    # Backward-compatible support for config/settings.json flat keys.
    return {
        "enabled": raw.get("enabled", raw.get("azure_openai_enabled", False)),
        "endpoint": raw.get("endpoint", raw.get("azure_openai_endpoint", "")),
        "api_key": raw.get("api_key", raw.get("azure_openai_api_key", "")),
        "deployment_name": raw.get("deployment_name", raw.get("azure_openai_deployment", "")),
        "api_version": raw.get("api_version", raw.get("azure_openai_api_version", "2024-02-01")),
        "temperature": raw.get("temperature", raw.get("azure_openai_temperature", 0.1)),
    }


def load_settings(config_path: str | None = None) -> SupervisorSettings:
    path = _resolve_settings_path(config_path)
    raw = _load_json_settings(path)
    azure = _extract_azure_settings(raw)

    env_overrides = {
        "enabled": os.getenv("AZURE_OPENAI_ENABLED"),
        "endpoint": os.getenv("AZURE_OPENAI_ENDPOINT"),
        "api_key": os.getenv("AZURE_OPENAI_API_KEY"),
        "deployment_name": os.getenv("AZURE_OPENAI_DEPLOYMENT"),
        "api_version": os.getenv("AZURE_OPENAI_API_VERSION"),
        "temperature": os.getenv("AZURE_OPENAI_TEMPERATURE"),
    }

    if env_overrides["enabled"] is not None:
        azure["enabled"] = env_overrides["enabled"].lower() in {"1", "true", "yes", "on"}
    if env_overrides["endpoint"]:
        azure["endpoint"] = env_overrides["endpoint"]
    if env_overrides["api_key"]:
        azure["api_key"] = env_overrides["api_key"]
    if env_overrides["deployment_name"]:
        azure["deployment_name"] = env_overrides["deployment_name"]
    if env_overrides["api_version"]:
        azure["api_version"] = env_overrides["api_version"]
    if env_overrides["temperature"]:
        azure["temperature"] = float(env_overrides["temperature"])

    return SupervisorSettings.model_validate({"azure_openai": azure})
