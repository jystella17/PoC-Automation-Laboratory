from __future__ import annotations

import re

from Supervisor.config import AzureOpenAISettings


class BaseLLM:
    """Shared base for all agent LLM wrappers.

    Provides common ``_create_llm``, ``is_available``, and
    ``_strip_code_fences`` so that each sub-agent LLM class only
    needs to implement its domain-specific prompts and parsing.
    """

    def __init__(self, settings: AzureOpenAISettings):
        self.settings = settings

    @property
    def is_available(self) -> bool:
        return self.settings.enabled and self.settings.is_configured

    def _create_llm(self):
        """Create an AzureChatOpenAI instance, or return *None*
        when the feature is disabled or the dependency is missing."""
        if not self.is_available:
            return None
        try:
            from langchain_openai import AzureChatOpenAI
        except ImportError:
            return None

        return AzureChatOpenAI(
            azure_endpoint=self.settings.endpoint,
            api_key=self.settings.api_key,
            azure_deployment=self.settings.deployment_name,
            api_version=self.settings.api_version,
            temperature=self.settings.temperature,
        )

    def _strip_code_fences(self, text: str) -> str:
        """Remove optional markdown code fences wrapping the response."""
        stripped = text.strip()
        match = re.match(r"```[a-zA-Z0-9_-]*\n(.*)\n```$", stripped, re.DOTALL)
        return match.group(1) if match else stripped
