"""
llm_provider.py
One interface for the cluster-doctor chatbot to talk to any of several LLM
backends (OpenAI, Anthropic, Ollama), selected via the
CLUSTER_DOCTOR_LLM_PROVIDER env var. If none is configured, or the
configured provider fails to initialize or to answer for any reason,
main.py falls back to the original, fully-offline rule-based assistant -
the chatbot is never left with no answer at all.

Env vars:
    CLUSTER_DOCTOR_LLM_PROVIDER   "openai" | "anthropic" | "ollama"  (unset = offline only)
    OPENAI_API_KEY / OPENAI_MODEL           (OpenAI)
    ANTHROPIC_API_KEY / ANTHROPIC_MODEL     (Anthropic)
    OLLAMA_HOST / OLLAMA_MODEL              (Ollama, local - no API key needed)
"""

from __future__ import annotations
import logging
import os
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger("cluster_doctor_host")


class LLMProvider(ABC):
    """One interface every backend implements, so main.py never needs to
    know which vendor it's talking to."""

    @abstractmethod
    def complete(self, system_prompt: str, user_message: str) -> str:
        """Return the model's reply as plain text. Must raise (any
        Exception) on failure - callers catch broadly and fall back to the
        offline rule-based assistant."""
        raise NotImplementedError


class OpenAIProvider(LLMProvider):
    def __init__(self):
        from openai import OpenAI  # imported lazily - dependency only needed if this provider is selected
        api_key = os.environ["OPENAI_API_KEY"]  # KeyError if missing -> caught by get_llm_provider()
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.client = OpenAI(api_key=api_key)

    def complete(self, system_prompt: str, user_message: str) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            max_tokens=400,
            timeout=15,
        )
        return resp.choices[0].message.content.strip()


class AnthropicProvider(LLMProvider):
    def __init__(self):
        from anthropic import Anthropic
        api_key = os.environ["ANTHROPIC_API_KEY"]
        self.model = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-haiku-20241022")
        self.client = Anthropic(api_key=api_key)

    def complete(self, system_prompt: str, user_message: str) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=400,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            timeout=15,
        )
        return resp.content[0].text.strip()


class OllamaProvider(LLMProvider):
    """Local Ollama server - no API key required, just needs Ollama running
    on the Host machine (or reachable on the LAN)."""

    def __init__(self):
        import requests  # noqa: F401 - just verifying it's importable at construction time
        self.host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        self.model = os.environ.get("OLLAMA_MODEL", "llama3")

    def complete(self, system_prompt: str, user_message: str) -> str:
        import requests
        resp = requests.post(
            f"{self.host}/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "stream": False,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()


_PROVIDERS = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "ollama": OllamaProvider,
}


def get_llm_provider() -> Optional[LLMProvider]:
    """Returns a constructed provider, or None if no provider is configured
    or the configured one fails to initialize (e.g. missing API key) - in
    which case main.py uses the offline rule-based assistant instead."""
    name = os.environ.get("CLUSTER_DOCTOR_LLM_PROVIDER", "").strip().lower()
    if not name:
        return None
    provider_cls = _PROVIDERS.get(name)
    if not provider_cls:
        logger.warning("Unknown CLUSTER_DOCTOR_LLM_PROVIDER=%r - using offline rule-based chatbot", name)
        return None
    try:
        return provider_cls()
    except Exception as exc:
        logger.warning(
            "Failed to initialize LLM provider %r (%s) - using offline rule-based chatbot", name, exc
        )
        return None
