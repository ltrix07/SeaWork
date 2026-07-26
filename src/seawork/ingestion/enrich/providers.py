"""Configuration-driven provider adapters for LLM enrichment."""
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportArgumentType=false

import json
from typing import cast

import httpx

from seawork.config import Settings


class OpenAICompatibleClient:
    """Small adapter for endpoints supporting JSON-schema constrained decoding."""

    def __init__(self, *, model_id: str, base_url: str, api_key: str, timeout: float) -> None:
        self.model_id = model_id
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    async def extract(self, prompt: str, schema: dict[str, object]) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self.model_id,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "enrichment", "strict": True, "schema": schema},
            },
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions", json=payload, headers=headers
            )
            response.raise_for_status()
        body = cast(dict[str, object], response.json())
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("provider response has no choices")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ValueError("provider response has no JSON content")
        parsed = json.loads(cast(str, message["content"]))
        if not isinstance(parsed, dict):
            raise ValueError("provider JSON response is not an object")
        return cast(dict[str, object], parsed)


def configured_client(settings: Settings) -> OpenAICompatibleClient | None:
    """Return no client for a disabled/incomplete provider, keeping ingest fail-open."""
    if not all(
        (settings.llm_provider, settings.llm_model_id, settings.llm_base_url, settings.llm_api_key)
    ):
        return None
    return OpenAICompatibleClient(
        model_id=settings.llm_model_id,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout_seconds,
    )
