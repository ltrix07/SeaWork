from pathlib import Path

from seawork.domain.enums import ExperienceLevel, OpportunityType, Provenance
from seawork.domain.models import NormalizedOpportunity
from seawork.ingestion.classify import Classification, classify
from seawork.ingestion.enrich.rules import RulesEnricher
from seawork.ingestion.quality import QualityResult, check_quality


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


def test_quality_rejects_short_description_and_duplicate() -> None:
    result = check_quality(opportunity(description="short"), duplicate_content=True)
    assert result.flags == ["description_too_short", "duplicate_content"]
    assert not result.accepted


def test_classify_prefers_declared_type(reference_dir: Path) -> None:
    result = classify(
        opportunity(title="Volunteer internship", declared_type="fixed_term_contract"),
        reference_dir / "opportunity_types.yaml",
    )
    assert result == Classification(OpportunityType.JOB, 1.0)


def test_enrichment_uses_source_fields_and_never_recruitment_office(
    reference_dir: Path,
) -> None:
    normalized = opportunity(
        title="SBN - Chef De Partie - CSSI",
        recruitment_office_raw="India - CSSI",
        location_raw=None,
        source_fields={
            "department": "Galley",
            "division": "Hotel",
            "skills_knowledge_expertise": "Minimum 5 years experience; STCW required.",
        },
    )
    enriched = RulesEnricher(reference_dir, direction="cruise", workplace="vessel").enrich(
        normalized, Classification(OpportunityType.JOB, 1.0), QualityResult(1.0, [])
    )
    assert enriched.country is None
    assert enriched.city is None
    assert enriched.profession and enriched.profession.value == "galley"
    assert enriched.direction and enriched.direction.provenance is Provenance.RULE
    assert enriched.required_certificates.value == ["stcw"]
    assert enriched.experience_level and enriched.experience_level.value is ExperienceLevel.SENIOR
    assert enriched.workplace_type_hint and enriched.workplace_type_hint.value == "vessel"


def test_no_experience_rule(reference_dir: Path) -> None:
    normalized = opportunity(source_fields={"skills_knowledge_expertise": "No prior experience"})
    enriched = RulesEnricher(reference_dir).enrich(
        normalized, Classification(OpportunityType.JOB, 0.5), QualityResult(1.0, [])
    )
    assert enriched.experience_level and enriched.experience_level.value is ExperienceLevel.ENTRY
