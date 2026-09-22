from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from seawork.domain.enums import OpportunityType
from seawork.domain.models import NormalizedOpportunity
from seawork.ingestion.classify import Classification
from seawork.ingestion.enrich.cache import SqlLLMCache
from seawork.ingestion.enrich.llm import LLMEnricher
from seawork.ingestion.enrich.rules import RulesEnricher
from seawork.ingestion.quality import QualityResult
from seawork.storage.models import Base


class FakeClient:
    model_id = "test-model"

    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, prompt: str, schema: dict[str, object]) -> dict[str, object]:
        self.calls += 1
        return {"experience_level": None, "required_certificates": []}


def test_sql_cache_persists_across_instances() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    key = ("a" * 64, "1", "test-model")
    value: dict[str, object] = {"experience_level": None, "required_certificates": []}

    with Session(engine) as first_session:
        SqlLLMCache(first_session).put(key, value)
    with Session(engine) as second_session:
        assert SqlLLMCache(second_session).get(key) == value


def test_enricher_reuses_sql_cache_after_new_session(reference_dir: Path) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    client = FakeClient()
    target = NormalizedOpportunity.model_validate(
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
    rules = RulesEnricher(reference_dir)

    for _ in range(2):
        with Session(engine) as session:
            LLMEnricher(rules, client, reference_dir, cache=SqlLLMCache(session)).enrich(
                target, classification, quality
            )

    assert client.calls == 1
