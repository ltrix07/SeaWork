"""Проверки на полном снапшоте живого Pinpoint API (93 вакансии).

Смысл этих тестов — не покрытие кода, а защита от тихой деградации разбора и
обогащения при изменении формата источника. Они же фиксируют фактические доли
заполняемости: расхождение с ними означает регрессию, а не повод править числа.
"""

import json
from pathlib import Path
from typing import cast

import pytest

from seawork.domain.enums import ExperienceLevel, Provenance
from seawork.ingestion.classify import classify
from seawork.ingestion.enrich.rules import RulesEnricher
from seawork.ingestion.quality import check_quality
from seawork.ingestion.sources.platforms.pinpoint import canonical_json

from tests.test_pinpoint import make_raw, make_source

SNAPSHOT = Path(__file__).parent / "fixtures" / "pinpoint" / "hollandamericagroup.json"
REFERENCE_DIR = Path(__file__).parents[1] / "data" / "reference"
CLASSIFICATION_RULES = REFERENCE_DIR / "opportunity_types.yaml"


@pytest.fixture(scope="module")
def postings() -> list[dict[str, object]]:
    document = cast(dict[str, object], json.loads(SNAPSHOT.read_text()))
    return cast(list[dict[str, object]], document["data"])


@pytest.fixture(scope="module")
def enriched(postings: list[dict[str, object]]) -> list[object]:
    source = make_source()
    enricher = RulesEnricher(REFERENCE_DIR, direction="cruise", workplace="vessel")
    results: list[object] = []
    for posting in postings:
        normalized = source.normalize(make_raw(posting))
        quality = check_quality(normalized, duplicate_content=False)
        classification = classify(normalized, CLASSIFICATION_RULES)
        results.append(enricher.enrich(normalized, classification, quality))
    return results


def test_snapshot_size(postings: list[dict[str, object]]) -> None:
    assert len(postings) == 93


def test_recruitment_office_never_becomes_job_location(enriched: list[object]) -> None:
    """Ловушка Pinpoint (contract §3.1): location — это офис найма, а не место работы.

    Источник заполняет location у 100% записей, и соблазн отобразить его в
    country/city велик. Отображение дало бы уверенно неверную географию:
    вакансия повара с офисом в Мумбаи — это работа на судне.
    """
    for item in enriched:
        opportunity = cast(object, item)
        assert getattr(opportunity, "country") is None
        assert getattr(opportunity, "city") is None
        # При этом сам офис не потерян — он сохранён в отдельном поле.
        assert getattr(getattr(opportunity, "normalized"), "recruitment_office_raw")


def test_posted_at_absent(enriched: list[object]) -> None:
    """У Pinpoint нет поля даты публикации (contract §3.2)."""
    for item in enriched:
        assert getattr(getattr(item, "normalized"), "posted_at") is None


def test_certificates_absent_means_unknown(enriched: list[object]) -> None:
    """Ненайденное — это None, а не пустой список (ingestion §3.3).

    Пустой список с confidence 0.9 читался бы как "сертификаты не требуются" и
    позволил бы Match Score построить ложное объяснение по SPEC §10.7.
    """
    for item in enriched:
        certificates = getattr(item, "required_certificates")
        if certificates is not None:
            assert certificates.value, "пустой список должен быть представлен как None"
            assert certificates.provenance is Provenance.RULE


def test_experience_ignores_recertification_periods(enriched: list[object]) -> None:
    """"N years" засчитывается только рядом со словом experience.

    В квалификациях встречается "Basic Food Hygiene course every 2 years" —
    это периодичность переаттестации, а не требуемый стаж.
    """
    for item in enriched:
        experience = getattr(item, "experience_level")
        if experience is None:
            continue
        assert isinstance(experience.value, ExperienceLevel)
        text = getattr(getattr(item, "normalized"), "source_fields")["skills_knowledge_expertise"]
        assert "experience" in (text or "").lower()


def _ratio(items: list[object], attribute: str) -> float:
    present = sum(1 for item in items if getattr(item, attribute) is not None)
    return present / len(items)


def test_coverage_matches_contract(enriched: list[object]) -> None:
    """Зафиксированные доли заполняемости из docs/modules/source-pinpoint.md §6."""
    assert _ratio(enriched, "country") == 0.0
    assert _ratio(enriched, "salary") == 0.0
    assert _ratio(enriched, "direction") == 1.0
    assert _ratio(enriched, "profession") >= 0.90
    assert 0.65 <= _ratio(enriched, "experience_level") <= 0.75
    assert 0.20 <= _ratio(enriched, "required_certificates") <= 0.35


def test_normalize_is_deterministic(postings: list[dict[str, object]]) -> None:
    """Один и тот же payload обязан давать один и тот же content_hash (§3.6)."""
    source = make_source()
    first = [canonical_json(posting) for posting in postings]
    second = [canonical_json(posting) for posting in postings]
    assert first == second
    assert len({source.normalize(make_raw(p)).external_id for p in postings}) == 93
