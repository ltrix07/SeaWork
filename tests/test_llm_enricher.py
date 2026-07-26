import logging
from pathlib import Path

from pytest import LogCaptureFixture, MonkeyPatch

from seawork.domain.enums import OpportunityType, Provenance
from seawork.domain.models import NormalizedOpportunity
from seawork.ingestion.classify import Classification
from seawork.ingestion.enrich.llm import LLMEnricher, MemoryLLMCache
from seawork.ingestion.enrich.rules import RulesEnricher
from seawork.ingestion.quality import QualityResult


class FakeClient:
    def __init__(self, response: dict[str, object], model_id: str = "test") -> None:
        self.response = response
        self.model_id = model_id
        self.calls = 0

    async def extract(self, prompt: str, schema: dict[str, object]) -> dict[str, object]:
        self.calls += 1
        return self.response


def opportunity(**updates: object) -> NormalizedOpportunity:
    values: dict[str, object] = {
        "source_id": "source",
        "external_id": "1",
        "url": "https://example.com/jobs/1",
        "title": "Chef",
        "description": "A" * 150,
    }
    values.update(updates)
    return NormalizedOpportunity.model_validate(values)


def enrich(
    client: FakeClient, reference_dir: Path, *, text: str = "Relevant vacancy requirement text."
):
    return LLMEnricher(RulesEnricher(reference_dir), client, reference_dir).enrich(
        opportunity(source_fields={"skills_knowledge_expertise": text}),
        Classification(OpportunityType.JOB, 1.0),
        QualityResult(1.0, []),
    )


def test_llm_discards_invented_quote(reference_dir: Path, caplog: LogCaptureFixture) -> None:
    client = FakeClient(
        {
            "experience_level": {"value": "mid", "basis": "stated", "quote": "invented"},
            "required_certificates": [],
        }
    )
    with caplog.at_level(logging.WARNING):
        result = enrich(client, reference_dir)
    assert result.experience_level is None
    assert "llm_quote_not_found" in caplog.text


def test_llm_null_is_a_valid_empty_answer(reference_dir: Path) -> None:
    result = enrich(
        FakeClient({"experience_level": None, "required_certificates": []}), reference_dir
    )
    assert result.experience_level is None
    assert result.required_certificates is None


def test_llm_drops_unknown_values(reference_dir: Path) -> None:
    result = enrich(
        FakeClient(
            {
                "experience_level": {"value": "senior-ish", "basis": "stated", "quote": "Relevant"},
                "required_certificates": [
                    {"key": "made_up", "basis": "stated", "quote": "Relevant"}
                ],
            }
        ),
        reference_dir,
    )
    assert result.experience_level is None
    assert result.required_certificates is None


def test_llm_never_overwrites_rules(reference_dir: Path) -> None:
    result = enrich(
        FakeClient(
            {
                "experience_level": {
                    "value": "entry",
                    "basis": "stated",
                    "quote": "Minimum 5 years",
                },
                "required_certificates": [],
            }
        ),
        reference_dir,
        text="Minimum 5 years experience",
    )
    assert result.experience_level is not None
    assert result.experience_level.value.value == "senior"
    assert result.experience_level.provenance is Provenance.RULE


def test_cache_key_contains_prompt_and_model(reference_dir: Path, monkeypatch: MonkeyPatch) -> None:
    import seawork.ingestion.enrich.llm as llm

    cache = MemoryLLMCache()
    rules = RulesEnricher(reference_dir)
    first = FakeClient({"experience_level": None, "required_certificates": []}, "one")
    target = opportunity(
        source_fields={"skills_knowledge_expertise": "Relevant vacancy requirement text."}
    )
    for client in (
        first,
        first,
        FakeClient({"experience_level": None, "required_certificates": []}, "two"),
    ):
        LLMEnricher(rules, client, reference_dir, cache=cache).enrich(
            target, Classification(OpportunityType.JOB, 1), QualityResult(1, [])
        )
    assert first.calls == 1
    monkeypatch.setattr(llm, "PROMPT_VERSION", "changed")
    changed = FakeClient({"experience_level": None, "required_certificates": []}, "one")
    LLMEnricher(rules, changed, reference_dir, cache=cache).enrich(
        target, Classification(OpportunityType.JOB, 1), QualityResult(1, [])
    )
    assert changed.calls == 1


def test_unavailable_provider_leaves_rules_result(reference_dir: Path) -> None:
    class Broken(FakeClient):
        async def extract(self, prompt: str, schema: dict[str, object]) -> dict[str, object]:
            raise OSError("offline")

    result = enrich(Broken({}), reference_dir)
    assert result.experience_level is None
    assert result.required_certificates is None
