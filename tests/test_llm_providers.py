import asyncio
import logging
from pathlib import Path

import httpx
import pytest
from pytest import LogCaptureFixture, MonkeyPatch

from seawork.domain.enums import OpportunityType
from seawork.domain.models import NormalizedOpportunity
from seawork.ingestion.classify import Classification
from seawork.ingestion.enrich.llm import LLMEnricher
from seawork.ingestion.enrich.providers import OpenAICompatibleClient
from seawork.ingestion.enrich.rules import RulesEnricher
from seawork.ingestion.quality import QualityResult


def provider_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": '{"experience_level": null, "required_certificates": []}'}}
            ]
        },
    )


def client(transport: httpx.AsyncBaseTransport, *, max_retries: int = 1):
    return OpenAICompatibleClient(
        model_id="test-model",
        base_url="https://provider.example/v1",
        api_key="secret",
        timeout=1.0,
        max_retries=max_retries,
        transport=transport,
    )


def extract(provider: OpenAICompatibleClient) -> dict[str, object]:
    return asyncio.run(provider.extract("prompt", {"type": "object"}))


def test_retries_server_error_then_succeeds() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503) if attempts == 1 else provider_response()

    result = extract(client(httpx.MockTransport(handler)))

    assert result == {"experience_level": None, "required_certificates": []}
    assert attempts == 2


def test_exhausted_retries_fail_open(reference_dir: Path, caplog: LogCaptureFixture) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    rules = RulesEnricher(reference_dir)
    opportunity = NormalizedOpportunity.model_validate(
        {
            "source_id": "source",
            "external_id": "1",
            "url": "https://example.com/jobs/1",
            "title": "Chef",
            "description": "A" * 150,
            "source_fields": {"skills_knowledge_expertise": "Relevant vacancy requirement text."},
        }
    )
    classification = Classification(OpportunityType.JOB, 1.0)
    quality = QualityResult(1.0, [])
    expected = rules.enrich(opportunity, classification, quality)

    with caplog.at_level(logging.WARNING):
        result = LLMEnricher(
            rules,
            client(httpx.MockTransport(handler)),
            reference_dir,
        ).enrich(opportunity, classification, quality)

    assert result == expected
    assert result.experience_level is None
    assert result.required_certificates is None
    assert attempts == 2
    assert "llm_unavailable" in caplog.text


def test_retries_invalid_body_then_succeeds() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, text="not json") if attempts == 1 else provider_response()

    result = extract(client(httpx.MockTransport(handler)))

    assert result == {"experience_level": None, "required_certificates": []}
    assert attempts == 2


def test_rate_limit_honours_retry_after(monkeypatch: MonkeyPatch) -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return provider_response()

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("seawork.ingestion.enrich.providers.asyncio.sleep", record_sleep)
    result = extract(client(httpx.MockTransport(handler)))

    assert result == {"experience_level": None, "required_certificates": []}
    assert delays == [2.0]


def test_does_not_retry_non_retryable_4xx() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401)

    with pytest.raises(httpx.HTTPStatusError):
        extract(client(httpx.MockTransport(handler), max_retries=3))

    assert attempts == 1
