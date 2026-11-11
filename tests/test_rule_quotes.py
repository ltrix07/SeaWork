import datetime
import hashlib
import json
import re
from pathlib import Path
from typing import cast

import pytest

from seawork.domain.enums import ExperienceLevel, OpportunityType, SourceTrust
from seawork.domain.inferred import Inferred
from seawork.domain.models import EnrichedOpportunity, NormalizedOpportunity
from seawork.ingestion.base import RawItem
from seawork.ingestion.classify import Classification, classify
from seawork.ingestion.enrich.llm import normalise_whitespace, quote_names_certificate
from seawork.ingestion.enrich.rules import RulesEnricher
from seawork.ingestion.quality import QualityResult, check_quality
from seawork.ingestion.references import load_yaml
from seawork.ingestion.sources.platforms.pinpoint import PinpointSource, canonical_json
from seawork.scoring.factors import _cite  # pyright: ignore[reportPrivateUsage]

REFERENCE_DIR = Path(__file__).parents[1] / "data" / "reference"


@pytest.fixture(scope="module")
def rules() -> RulesEnricher:
    return RulesEnricher(REFERENCE_DIR)


def _enrich(text: str, rules: RulesEnricher, title: str = "Chef") -> EnrichedOpportunity:
    opportunity = NormalizedOpportunity.model_validate(
        {
            "source_id": "source",
            "external_id": "1",
            "url": "https://example.com/jobs/1",
            "title": title,
            "source_fields": {"skills_knowledge_expertise": text},
        }
    )
    return rules.enrich(
        opportunity, Classification(OpportunityType.JOB, 1.0), QualityResult(1.0, [])
    )


def _experience(text: str, rules: RulesEnricher) -> Inferred[ExperienceLevel] | None:
    return _enrich(text, rules).experience_level


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


def _certificate_patterns() -> dict[str, list[str]]:
    rows = cast(
        list[dict[str, object]], load_yaml(REFERENCE_DIR / "certificates.yaml")["certificates"]
    )
    return {str(row["key"]): cast(list[str], row["patterns"]) for row in rows}


def _language_aliases() -> dict[str, list[str]]:
    rows = cast(list[dict[str, object]], load_yaml(REFERENCE_DIR / "languages.yaml")["languages"])
    return {str(row["key"]): cast(list[str], row["aliases"]) for row in rows}


def _verbatim(field: Inferred[list[str]] | None, source: str) -> dict[str, str]:
    assert field is not None
    assert field.quotes is not None
    assert list(field.quotes) == field.value
    for quote in field.quotes.values():
        assert normalise_whitespace(quote) in normalise_whitespace(source)
        assert len(quote) <= 80
    return field.quotes


def test_required_language_quote_is_the_sentence_with_the_marker(rules: RulesEnricher) -> None:
    text = "Valid passport. Ability to read, write and speak English. Own tools."
    field = _enrich(text, rules).required_languages
    assert _verbatim(field, text) == {"en": "Ability to read, write and speak English"}


def test_preferred_language_quote_is_the_sentence_with_the_marker(rules: RulesEnricher) -> None:
    text = "Marlins Score of 90+; fluency in Dutch or German is advantageous."
    field = _enrich(text, rules).preferred_languages
    assert _verbatim(field, text) == {
        "nl": "fluency in Dutch or German is advantageous",
        "de": "fluency in Dutch or German is advantageous",
    }


def test_title_language_quote_is_a_substring_of_the_title(rules: RulesEnricher) -> None:
    title = "Bar Steward (Japanese Speaking) - CS"
    field = _enrich("Serve guests at the bar.", rules, title).required_languages
    assert field is not None
    assert field.quotes == {"ja": "Japanese Speaking"}


def test_enumerated_languages_have_no_quote_because_they_have_no_field(
    rules: RulesEnricher,
) -> None:
    text = "Knowledge of another language such as:\nDutch, Spanish, German"
    enriched = _enrich(text, rules)
    assert enriched.required_languages is None and enriched.preferred_languages is None


def test_language_stated_as_required_is_not_quoted_as_preferred(rules: RulesEnricher) -> None:
    text = "Must speak English. French is an advantage."
    enriched = _enrich(text, rules)
    assert enriched.required_languages is not None
    assert enriched.required_languages.quotes == {"en": "Must speak English"}
    assert enriched.preferred_languages is not None
    assert enriched.preferred_languages.quotes == {"fr": "French is an advantage"}


def test_preferred_phrase_first_does_not_leave_the_language_preferred(
    rules: RulesEnricher,
) -> None:
    text = "Fluency in English is an advantage. Must speak English."
    enriched = _enrich(text, rules)
    assert enriched.preferred_languages is None
    assert enriched.required_languages is not None
    assert enriched.required_languages.quotes == {"en": "Must speak English"}


def test_certificate_quote_comes_from_the_earliest_mention_of_any_pattern(
    rules: RulesEnricher,
) -> None:
    text = "Candidates must complete BST before joining. Valid STCW certificate required."
    patterns = _certificate_patterns()["stcw"]
    assert patterns.index("STCW") < patterns.index("BST")
    field = _enrich(text, rules).required_certificates
    assert _verbatim(field, text) == {"stcw": "Candidates must complete BST before joining"}


def test_each_certificate_has_its_own_quote_naming_it(rules: RulesEnricher) -> None:
    text = "Valid STCW Basic Safety certificate required.\nMust hold a valid passport. Own tools."
    field = _enrich(text, rules).required_certificates
    quotes = _verbatim(field, text)
    patterns = _certificate_patterns()
    assert set(quotes) == {"stcw", "passport"}
    for key, quote in quotes.items():
        assert quote_names_certificate(patterns[key], quote)
    assert quotes["passport"] == "Must hold a valid passport"


def test_rule_quotes_reach_the_match_score_explanation(rules: RulesEnricher) -> None:
    text = "Valid STCW Basic Safety certificate required. Must hold a valid passport."
    enriched = _enrich(text, rules)
    assert enriched.required_certificates is not None
    assert _cite(enriched.required_certificates) == (
        "stcw: Valid STCW Basic Safety certificate required, "
        "passport: Must hold a valid passport [rule]"
    )
    spoken = _enrich("Must speak English.", rules).required_languages
    assert spoken is not None
    assert _cite(spoken) == "en: Must speak English [rule]"


PINPOINT = Path(__file__).parent / "fixtures" / "pinpoint"


@pytest.mark.parametrize("account", ["hollandamericagroup", "princesscruises"])
def test_every_quote_on_the_snapshot_is_verbatim_and_names_its_key(account: str) -> None:
    document = cast(dict[str, object], json.loads((PINPOINT / f"{account}.json").read_text()))
    source = PinpointSource(
        account=account,
        source_id=f"pinpoint:{account}",
        trust=SourceTrust.PRIMARY,
        user_agent="SeaWork test (contact: test@example.com)",
        interval_seconds=0,
    )
    enricher = RulesEnricher(REFERENCE_DIR, direction="cruise", workplace="vessel")
    certificates = _certificate_patterns()
    languages = _language_aliases()
    quoted = 0
    for posting in cast(list[dict[str, object]], document["data"]):
        payload = canonical_json(posting)
        raw = RawItem.model_validate(
            {
                "source_id": f"pinpoint:{account}",
                "external_id": str(posting["id"]),
                "url": str(posting["url"]),
                "fetched_at": datetime.datetime.now(datetime.UTC),
                "payload": payload,
                "content_type": "application/json",
                "content_hash": hashlib.sha256(payload.encode()).hexdigest(),
                "http_status": 200,
            }
        )
        normalized = source.normalize(raw)
        enriched = enricher.enrich(
            normalized,
            classify(normalized, REFERENCE_DIR / "opportunity_types.yaml"),
            check_quality(normalized, duplicate_content=False),
        )
        text = normalise_whitespace(enricher.qualification_text(normalized))
        title = normalise_whitespace(normalized.title)
        for field in (enriched.required_languages, enriched.preferred_languages):
            if field is not None:
                assert field.quotes is not None and list(field.quotes) == field.value
                for key, quote in field.quotes.items():
                    from_title = any(
                        re.fullmatch(rf"{re.escape(alias)}[\s-]*speaking", quote, re.IGNORECASE)
                        for alias in languages[key]
                    )
                    assert quote in title if from_title else normalise_whitespace(quote) in text
                    assert any(
                        re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", quote, re.IGNORECASE)
                        for alias in languages[key]
                    )
                    assert len(quote) <= 80
                    quoted += 1
        if enriched.required_certificates is not None:
            quotes = enriched.required_certificates.quotes
            assert quotes is not None and list(quotes) == enriched.required_certificates.value
            for key, quote in quotes.items():
                assert normalise_whitespace(quote) in text
                assert len(quote) <= 80
                assert quote_names_certificate(certificates[key], quote)
                quoted += 1
    assert quoted > 0
