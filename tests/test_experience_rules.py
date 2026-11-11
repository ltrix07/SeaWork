from pathlib import Path

import pytest

from seawork.domain.enums import ExperienceLevel, OpportunityType
from seawork.domain.models import NormalizedOpportunity
from seawork.ingestion.classify import Classification
from seawork.ingestion.enrich.rules import RulesEnricher
from seawork.ingestion.quality import QualityResult

REFERENCE_DIR = Path(__file__).parents[1] / "data" / "reference"


@pytest.fixture(scope="module")
def rules() -> RulesEnricher:
    return RulesEnricher(REFERENCE_DIR)


def _level(text: str, rules: RulesEnricher) -> tuple[ExperienceLevel, str] | None:
    opportunity = NormalizedOpportunity.model_validate(
        {
            "source_id": "source",
            "external_id": "1",
            "url": "https://example.com/jobs/1",
            "title": "Chef",
            "source_fields": {"skills_knowledge_expertise": text},
        }
    )
    experience = rules.enrich(
        opportunity, Classification(OpportunityType.JOB, 1.0), QualityResult(1.0, [])
    ).experience_level
    if experience is None:
        return None
    assert experience.quotes is not None
    return experience.value, experience.quotes[str(experience.value)]


@pytest.mark.parametrize(
    ("text", "level"),
    [
        ("Minimum 2 years of experience", ExperienceLevel.MID),
        ("Min 6 months in rank most recent experience", ExperienceLevel.JUNIOR),
        (
            "Min 12 months experience on a similar ship; 2 years of experience as cook",
            ExperienceLevel.MID,
        ),
        ("5+ years of experience", ExperienceLevel.SENIOR),
        ("Min.3 years of experience", ExperienceLevel.MID),
        ("experience,5 years", ExperienceLevel.SENIOR),
        ("Experience.6 months", ExperienceLevel.JUNIOR),
    ],
)
def test_plain_tenure_is_read_as_before(
    text: str, level: ExperienceLevel, rules: RulesEnricher
) -> None:
    result = _level(text, rules)
    assert result is not None
    assert result[0] == level


@pytest.mark.parametrize(
    ("text", "level"),
    [
        ("At least 2.5 years of experience", ExperienceLevel.MID),
        ("At least 1,5 years of experience", ExperienceLevel.JUNIOR),
        ("At least 4.5 years of experience", ExperienceLevel.MID),
        ("Опыт 1,5 лет", ExperienceLevel.JUNIOR),
        ("Experience 12.5 years", ExperienceLevel.SENIOR),
    ],
)
def test_fraction_is_one_number(text: str, level: ExperienceLevel, rules: RulesEnricher) -> None:
    result = _level(text, rules)
    assert result is not None
    assert result[0] == level


@pytest.mark.parametrize(
    ("text", "level", "quote"),
    [
        ("4-5 years of experience in rank", ExperienceLevel.MID, "4-5 years of experience in rank"),
        ("4 \u2013 5 years of experience", ExperienceLevel.MID, "4 \u2013 5 years of experience"),
        ("4 - 5 years of experience", ExperienceLevel.MID, "4 - 5 years of experience"),
        ("Опыт 2 - 3 года", ExperienceLevel.MID, "Опыт 2 - 3 года"),
        ("1 to 2 years of experience", ExperienceLevel.JUNIOR, "1 to 2 years of experience"),
        ("5-7+ years of experience", ExperienceLevel.SENIOR, "5-7+ years of experience"),
        ("опыт от 2 до 3 лет", ExperienceLevel.MID, "опыт от 2 до 3 лет"),
        ("Опыт работы 1\u20143 года", ExperienceLevel.JUNIOR, "Опыт работы 1\u20143 года"),
    ],
)
def test_range_is_its_lower_bound(
    text: str, level: ExperienceLevel, quote: str, rules: RulesEnricher
) -> None:
    assert _level(text, rules) == (level, quote)


@pytest.mark.parametrize(
    "text",
    [
        "Valid experience. Food Hygiene refresher every 10 years",
        "Experience needed. Course renewed each 2 years",
        "Опыт обязателен. Медкомиссия каждые 2 года",
        "Опыт обязателен. Обучение раз в 5 лет",
    ],
)
def test_renewal_cycle_is_not_tenure(text: str, rules: RulesEnricher) -> None:
    assert _level(text, rules) is None


def test_cycle_does_not_hide_real_tenure(rules: RulesEnricher) -> None:
    text = "Refresher every 2 years. Minimum 6 years of experience"
    result = _level(text, rules)
    assert result is not None
    assert result[0] == ExperienceLevel.SENIOR


def test_bare_upper_bound_is_unchanged(rules: RulesEnricher) -> None:
    assert _level("опыт до 3 лет", rules) == (ExperienceLevel.MID, "опыт до 3 лет")


@pytest.mark.parametrize(
    "text",
    [
        "Experience required. Age between 25 and 38 years old",
        "Experience required. Candidates 30 years old",
        "Experience required. Must be 25 years of age",
        "Experience required. Aged 25-45 years",
        "Experience required. Age: 25 years",
        "Опыт обязателен. Возраст 25 лет",
        "Experience required. AED credentials (within last 2 years)",
        "Experience required. Certificate issued within the last 5 years",
        "Experience required. Worked in the past 3 years",
    ],
)
def test_age_and_recency_are_not_tenure(text: str, rules: RulesEnricher) -> None:
    assert _level(text, rules) is None


@pytest.mark.parametrize(
    ("text", "level"),
    [
        ("management experience of 3 years", ExperienceLevel.MID),
        ("2 years of experience in the last company", ExperienceLevel.MID),
        ("Stage manager experience 6 years", ExperienceLevel.SENIOR),
        ("Experience in the last role, minimum 3 years", ExperienceLevel.MID),
        ("Age 18+, minimum 2 years of experience", ExperienceLevel.MID),
    ],
)
def test_age_and_recency_words_inside_other_words_do_not_hide_tenure(
    text: str, level: ExperienceLevel, rules: RulesEnricher
) -> None:
    result = _level(text, rules)
    assert result is not None
    assert result[0] == level
