from __future__ import annotations

from langchain_openai import ChatOpenAI

from app.config import Settings
from app.security import contains_pii


class ModelRouter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def model_names(self, content: str = "") -> list[str]:
        primary = self.settings.chat_model
        if content and contains_pii(content) and self.settings.sensitive_chat_model:
            primary = self.settings.sensitive_chat_model
        return list(dict.fromkeys([primary, *self.settings.chat_fallback_model_list]))

    def build(self, content: str = ""):
        models = [self._chat_model(name) for name in self.model_names(content)]
        return models[0].with_fallbacks(models[1:]) if len(models) > 1 else models[0]

    def _chat_model(self, name: str) -> ChatOpenAI:
        kwargs: dict = {
            "model": name,
            "api_key": self.settings.openai_api_key,
            "temperature": 0,
            "timeout": self.settings.model_timeout_seconds,
            "max_retries": self.settings.model_max_retries,
            "stream_usage": True,
        }
        if self.settings.openai_base_url:
            kwargs["base_url"] = self.settings.openai_base_url
        return ChatOpenAI(**kwargs)
