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


def test_notes_are_kept_without_rejecting_the_record() -> None:
    """A note records a fact; a flag rejects. Conflating them loses one or the other.

    Before the split, a source with something to say about its own record had two
    options: put it in `flags` and lose the record, or put it nowhere and lose the
    fact. PADI's trashed postings hit exactly that and were parked in source_fields.
    """
    clean = check_quality(opportunity(description="x" * 200))
    noted = check_quality(opportunity(description="x" * 200), notes=["source_trashed"])
    assert noted.accepted
    assert noted.notes == ["source_trashed"]
    assert not noted.flags
    # A fact about provenance is not a quality defect, so the score must not move.
    assert noted.score == clean.score


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
    assert enriched.required_certificates and enriched.required_certificates.value == ["stcw"]
    assert enriched.experience_level and enriched.experience_level.value is ExperienceLevel.SENIOR
    assert enriched.workplace_type_hint and enriched.workplace_type_hint.value == "vessel"


def test_no_experience_rule(reference_dir: Path) -> None:
    normalized = opportunity(source_fields={"skills_knowledge_expertise": "No prior experience"})
    enriched = RulesEnricher(reference_dir).enrich(
        normalized, Classification(OpportunityType.JOB, 0.5), QualityResult(1.0, [])
    )
    assert enriched.experience_level and enriched.experience_level.value is ExperienceLevel.ENTRY


def test_language_markers_are_read_in_the_right_scope(reference_dir: Path) -> None:
    """The three phrasings that decide which language field gets filled.

    Every case here was a real misclassification on the snapshots before the rule
    took its current shape, so the test is a record of what actually goes wrong
    rather than a guess at what might.
    """
    enricher = RulesEnricher(reference_dir, direction="cruise", workplace="vessel")

    # A section label belonging to the NEXT item must not reach the language.
    required, preferred = enricher._languages_from(  # pyright: ignore[reportPrivateUsage]
        "Cook",
        "Required: Ability to read, write and speak English.\nPreferred: Degree from a college.",
    )
    assert required == ["en"]
    assert preferred == []

    # An enumeration of examples fills neither field, even when the markup splits it
    # across lines and a requirement for something else follows.
    required, preferred = enricher._languages_from(  # pyright: ignore[reportPrivateUsage]
        "Guest Services Officer",
        "Knowledge of another language such as:\nDutch, Spanish, German\n"
        "French, Russian, Italian Must hold a valid STCW certificate.",
    )
    assert required == []
    assert preferred == []

    # A preference stays a preference: the sentence carries both "fluency" and
    # "advantageous", and reading it as a requirement is the damaging error.
    required, preferred = enricher._languages_from(  # pyright: ignore[reportPrivateUsage]
        "Front Desk Manager",
        "Marlins Score of 90+; fluency in Dutch or German is advantageous.",
    )
    assert required == []
    assert preferred == ["nl", "de"]

    # The title is the strongest signal and needs no marker in the body at all.
    required, preferred = enricher._languages_from(  # pyright: ignore[reportPrivateUsage]
        "Bar Steward (Japanese Speaking) - CS", "Serve guests at the bar."
    )
    assert required == ["ja"]
