from pathlib import Path

import pytest

from seawork.domain.enums import ExperienceLevel, OpportunityType
from seawork.domain.inferred import Inferred
from seawork.domain.models import NormalizedOpportunity
from seawork.ingestion.classify import Classification
from seawork.ingestion.enrich.llm import normalise_whitespace
from seawork.ingestion.enrich.rules import RulesEnricher
from seawork.ingestion.quality import QualityResult

REFERENCE_DIR = Path(__file__).parents[1] / "data" / "reference"


@pytest.fixture(scope="module")
def rules() -> RulesEnricher:
    return RulesEnricher(REFERENCE_DIR)


def _experience(text: str, rules: RulesEnricher) -> Inferred[ExperienceLevel] | None:
    opportunity = NormalizedOpportunity.model_validate(
        {
            "source_id": "source",
            "external_id": "1",
            "url": "https://example.com/jobs/1",
            "title": "Chef",
            "source_fields": {"skills_knowledge_expertise": text},
        }
    )
    return rules.enrich(
        opportunity, Classification(OpportunityType.JOB, 1.0), QualityResult(1.0, [])
    ).experience_level


def _quote(text: str, key: str, rules: RulesEnricher) -> str:
    experience = _experience(text, rules)
    assert experience is not None
    assert experience.quotes is not None
    quote = experience.quotes[key]
    assert normalise_whitespace(quote) in normalise_whitespace(text)
    assert len(quote) <= 80
    return quote


def test_entry_quote_is_the_sentence(rules: RulesEnricher) -> None:
    text = "Must hold a valid passport. No prior experience required. Good English."
    assert _quote(text, "entry", rules) == "No prior experience required"


def test_years_quote_comes_from_the_strictest_match(rules: RulesEnricher) -> None:
    text = (
        "Minimum 2 years of experience in a similar role. "
        "Must have at least 6 years of experience in large Butchery operations."
    )
    experience = _experience(text, rules)
    assert experience is not None
    assert experience.value == ExperienceLevel.SENIOR
    assert _quote(text, "senior", rules) == (
        "Must have at least 6 years of experience in large Butchery operations"
    )


def test_months_winner_is_quoted(rules: RulesEnricher) -> None:
    text = "Requirements:\nMin 6 months in rank most recent experience on a similar ship\nSTCW"
    assert (
        _quote(text, "junior", rules)
        == "Min 6 months in rank most recent experience on a similar ship"
    )


def test_long_sentence_is_cut_to_a_window_on_word_boundaries(rules: RulesEnricher) -> None:
    text = (
        "Candidates applying for this position should be reliable, hardworking and "
        "motivated team players who have at least 3 years of experience in busy "
        "hotel kitchens on board large passenger vessels worldwide"
    )
    quote = _quote(text, "mid", rules)
    assert "3 years of experience" in quote
    assert len(quote) > 40
    assert quote[0] != " " and quote[-1] != " "
    assert all(word in text.split() for word in quote.split())


def test_whitespace_in_text_is_collapsed_in_the_quote(rules: RulesEnricher) -> None:
    text = "Must have\xa0at least   5 years\n of experience as a Chef de Partie"
    quote = _quote(text, "senior", rules)
    assert "  " not in quote


def test_strictest_match_wins_regardless_of_order(rules: RulesEnricher) -> None:
    text = (
        "Must have at least 6 years of experience in large Butchery operations. "
        "Minimum 2 years of experience in a similar role."
    )
    assert _quote(text, "senior", rules) == (
        "Must have at least 6 years of experience in large Butchery operations"
    )


def test_months_and_years_pick_the_years_winner(rules: RulesEnricher) -> None:
    text = "Min 12 months experience on a similar ship; at least 2 years of experience as cook"
    experience = _experience(text, rules)
    assert experience is not None
    assert experience.value == ExperienceLevel.MID
    assert _quote(text, "mid", rules) == "at least 2 years of experience as cook"


def test_discarded_larger_number_is_not_quoted(rules: RulesEnricher) -> None:
    text = (
        "Food Hygiene course must be renewed every 10 years by all crew members on board.\n"
        "Candidates need valid medical certificates and the right to work in the EU.\n"
        "At least 3 years of experience as cook"
    )
    experience = _experience(text, rules)
    assert experience is not None
    assert experience.value == ExperienceLevel.MID
    assert "3 years" in _quote(text, "mid", rules)
