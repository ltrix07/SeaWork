from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from seawork.domain.enums import ExperienceLevel, OpportunityType
from seawork.domain.inferred import Inferred


class NormalizedOpportunity(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str
    external_id: str
    url: HttpUrl
    title: str
    description: str | None = None
    employer: str | None = None
    location_raw: str | None = None
    recruitment_office_raw: str | None = None
    salary_raw: str | None = None
    posted_at: datetime | None = None
    declared_type: str | None = None
    language: str | None = None
    source_fields: dict[str, str | None] = Field(default_factory=dict)


class SalaryRange(BaseModel):
    model_config = ConfigDict(frozen=True)

    minimum: Decimal | None = None
    maximum: Decimal | None = None
    currency: str
    period: str | None = None


class HiringScope(BaseModel):
    """Where a vacancy hires from, as its recruitment office states it.

    Three cases, because the field holds three different kinds of answer and
    flattening them loses the one that matters most. `anywhere` is "Global", which
    130 of 447 Pinpoint records carry: it is not missing data but a statement that
    the employer hires from everywhere, and it must match every user rather than
    none. `countries` are offices naming a country. `unresolved` keeps regional
    wordings - "Caribbean", "Central & South America" - as written, because mapping
    a region onto countries is a guess we have no table for and no mandate to make.
    """

    model_config = ConfigDict(frozen=True)

    anywhere: bool = False
    countries: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


class EnrichedOpportunity(BaseModel):
    model_config = ConfigDict(frozen=True)

    normalized: NormalizedOpportunity
    type: OpportunityType
    type_confidence: float = Field(ge=0.0, le=1.0)
    # Four geography fields, all optional, one schema for shore and vessel work
    # (docs/modules/ingestion.md 4.3.1). A vessel contract has no country of work in
    # the shore sense - every source measures 0% - so it fills the two below instead.
    # country/city stay for shore sources, which the spec anticipates.
    country: Inferred[str] | None = None
    city: Inferred[str] | None = None
    # Where the person flies to join, and where the vessel operates. Separate fields:
    # joining in Barcelona happens on a Mediterranean season and on a transatlantic
    # crossing alike, and for the seafarer those are different contracts.
    embarkation_port: Inferred[str] | None = None
    operating_region: Inferred[str] | None = None
    # Where hiring happens, which is the only geography a vessel source states at all
    # (A4). Matched against the profile's `hiring_country`, not against a work country.
    hiring_scope: Inferred[HiringScope] | None = None
    profession: Inferred[str] | None = None
    direction: Inferred[str] | None = None
    required_certificates: Inferred[list[str]] | None = None
    # Two fields, not one. A required language the candidate lacks must lower Match
    # Score; a preferred one they happen to have may raise it, and its absence must
    # not be punished. Merged into a single field, "Dutch is mandatory" and "Dutch is
    # advantageous" become indistinguishable and one of the two behaviours is wrong.
    required_languages: Inferred[list[str]] | None = None
    preferred_languages: Inferred[list[str]] | None = None
    experience_level: Inferred[ExperienceLevel] | None = None
    salary: Inferred[SalaryRange] | None = None
    workplace_type_hint: Inferred[str] | None = None
    quality_score: float = Field(ge=0.0, le=1.0)
    quality_flags: list[str]
    # Reasons to reject versus facts to keep; see ingestion/quality.py.
    quality_notes: list[str] = Field(default_factory=list)
