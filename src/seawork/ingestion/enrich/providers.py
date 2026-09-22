"""Configuration-driven provider adapters for LLM enrichment."""
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportArgumentType=false

import asyncio
import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import cast

import httpx

from seawork.config import Settings


class OpenAICompatibleClient:
    """Small adapter for endpoints supporting JSON-schema constrained decoding."""

    def __init__(
        self,
        *,
        model_id: str,
        base_url: str,
        api_key: str,
        timeout: float,
        max_retries: int,
        temperature: float | None = 0.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model_id = model_id
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._max_retries = max(0, max_retries)
        self._temperature = temperature
        self._transport = transport

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())

    @staticmethod
    def _parse_response(response: httpx.Response) -> dict[str, object]:
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("provider response is not an object")
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

    async def extract(self, prompt: str, schema: dict[str, object]) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self.model_id,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "enrichment", "strict": True, "schema": schema},
            },
        }
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            for attempt in range(self._max_retries + 1):
                response: httpx.Response | None = None
                try:
                    response = await client.post(
                        f"{self._base_url}/chat/completions", json=payload, headers=headers
                    )
                    response.raise_for_status()
                    return self._parse_response(response)
                except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as exc:
                    if isinstance(exc, httpx.HTTPStatusError) and not (
                        exc.response.status_code == 429 or exc.response.status_code >= 500
                    ):
                        raise
                    if attempt >= self._max_retries:
                        raise
                    delay = min(8.0, 0.5 * 2**attempt)
                    if response is not None and response.status_code == 429:
                        retry_after = self._retry_after(response)
                        if retry_after is not None:
                            # Cap a server-supplied wait so a hostile or misconfigured
                            # Retry-After cannot stall ingest indefinitely.
                            delay = min(60.0, retry_after)
                    await asyncio.sleep(delay)
        raise RuntimeError("LLM retry loop finished without a result")


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
        max_retries=settings.llm_max_retries,
        temperature=settings.llm_temperature,
    )
