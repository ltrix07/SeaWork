import re
from decimal import Decimal
from pathlib import Path
from typing import cast

from seawork.domain.enums import ExperienceLevel, Provenance
from seawork.domain.inferred import Inferred
from seawork.domain.models import EnrichedOpportunity, NormalizedOpportunity, SalaryRange
from seawork.ingestion.classify import Classification
from seawork.ingestion.quality import QualityResult
from seawork.ingestion.references import load_yaml

# Character window used to look for the word "experience" around an "N years" match.
_EXPERIENCE_WINDOW = 60
# Narrower window checked immediately before the number for contract-length wording.
_DURATION_WINDOW = 30
_DURATION_WORDING = r"duration|contract|on/off|trip|voyage|sign[- ]?on|rotation"


def parse_salary(raw: str | None) -> Inferred[SalaryRange] | None:
    if not raw:
        return None
    compact = " ".join(raw.split())
    match = re.fullmatch(
        r"(?:(?P<upper>up to) )?"
        r"(?P<first>\d+(?: \d{3})*)"
        r"(?: - (?P<second>\d+(?: \d{3})*))? "
        r"(?P<currency>USD|EUR|GBP)"
        r"(?P<daily> per day)?",
        compact,
        re.IGNORECASE,
    )
    if match is None:
        return None
    first = Decimal(match.group("first").replace(" ", ""))
    second_raw = match.group("second")
    second = Decimal(second_raw.replace(" ", "")) if second_raw else None
    if match.group("upper") and second is not None:
        return None
    minimum = None if match.group("upper") else first
    maximum = second or first
    is_daily = match.group("daily") is not None
    return Inferred(
        value=SalaryRange(
            minimum=minimum,
            maximum=maximum,
            currency=match.group("currency").upper(),
            period="day" if is_daily else "month",
        ),
        provenance=Provenance.RULE,
        confidence=1.0 if is_daily else 0.8,
    )


class RulesEnricher:
    def __init__(
        self, reference_dir: Path, *, direction: str | None = None, workplace: str | None = None
    ):
        self._professions = load_yaml(reference_dir / "professions.yaml")
        self._certificates = load_yaml(reference_dir / "certificates.yaml")
        self._countries = load_yaml(reference_dir / "countries.yaml")
        self._direction = direction
        self._workplace = workplace

    def enrich(
        self,
        opportunity: NormalizedOpportunity,
        classification: Classification,
        quality: QualityResult,
    ) -> EnrichedOpportunity:
        structured_profession_text = " ".join(
            part
            for part in [
                opportunity.source_fields.get("department"),
                opportunity.source_fields.get("division"),
            ]
            if part
        )
        qualification_text = self.qualification_text(opportunity)
        return EnrichedOpportunity(
            normalized=opportunity,
            type=classification.type,
            type_confidence=classification.confidence,
            # A shipboard contract does not have a job country in the shore-side
            # sense; a mentioned location is retained verbatim but not promoted.
            country=(
                None if self._workplace == "vessel" else self._country(opportunity.location_raw)
            ),
            city=None,
            profession=self._profession_from_source(
                structured_profession_text, opportunity.title
            ),
            direction=(
                Inferred(value=self._direction, provenance=Provenance.RULE, confidence=1.0)
                if self._direction
                else None
            ),
            required_certificates=self._required_certificates(qualification_text),
            experience_level=self._experience(qualification_text),
            salary=parse_salary(opportunity.salary_raw),
            workplace_type_hint=(
                Inferred(value=self._workplace, provenance=Provenance.RULE, confidence=1.0)
                if self._workplace
                else None
            ),
            quality_score=quality.score,
            quality_flags=quality.flags,
        )

    def _profession_from_source(self, structured: str, title: str) -> Inferred[str] | None:
        """Match on the structured department first, then fall back to the title.

        Structured fields are the better signal where a source fills them with a
        vocabulary we know, so they are tried first and keep the higher confidence.
        But each employer names its departments differently — Princess Cruises uses
        "Rooms Division", "FB Svc", "Shorex", none of which Holland America's
        taxonomy contains — and a department that fails to match used to end the
        search, leaving an informative title ("Fitter Mechanic") unread.

        The fallback is only safe because professions.yaml lists compound titles in
        full. Enabling it against a reference file that has a bare "master" alias
        maps every "Provision Master" to the ship's captain.
        """
        if structured:
            matched = self._profession(structured)
            if matched is not None:
                return matched
        # A title is free text written for humans, so a match there is worth less
        # than one against a field the employer filled from a controlled list.
        fallback = self._profession(title)
        if fallback is None:
            return None
        return Inferred(value=fallback.value, provenance=Provenance.RULE, confidence=0.75)

    def _profession(self, text: str) -> Inferred[str] | None:
        # The longest matching alias wins, not the first one in the file. "Chief Officer"
        # must beat the generic "officer" regardless of where either sits in the YAML:
        # the reference files are maintained by a domain expert, and correctness must not
        # depend on line order.
        folded = text.casefold()
        best_key: str | None = None
        best_length = 0
        rows_value = self._professions.get("professions", [])
        rows = cast(list[object], rows_value) if isinstance(rows_value, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = cast(dict[str, object], row)
            key = item.get("key")
            if not isinstance(key, str):
                continue
            aliases = item.get("aliases", [])
            alias_values = cast(list[object], aliases) if isinstance(aliases, list) else []
            for alias in alias_values:
                if not isinstance(alias, str):
                    continue
                if alias.casefold() in folded and len(alias) > best_length:
                    best_key, best_length = key, len(alias)
        if best_key is None:
            return None
        return Inferred(value=best_key, provenance=Provenance.RULE, confidence=0.9)

    @staticmethod
    def qualification_text(opportunity: NormalizedOpportunity) -> str:
        """Return every text a source offers that may state hiring requirements.

        Sources publish requirement-bearing text under agreed keys; the enricher
        never learns source-specific field names (3.5). Adding a key here is how a
        source makes newly-found text reachable.

        Deliberately excluded: text describing the duties of the role. Those
        mention standards the job is performed under ("in accordance with USPH
        standards", "following HACCP guidelines"), which read like certificate
        requirements but are not — the candidate is not asked to hold them. On the
        Pinpoint snapshot that text would have produced 40 false certificate fills,
        the exact failure this milestone exists to prevent.

        Kept as a separate method so scripts/audit_payload_coverage.py measures the
        same text the enricher reads, rather than a copy that can drift from it.
        """
        parts = [
            opportunity.source_fields.get(key)
            for key in ("skills_knowledge_expertise", "additional_requirements")
        ]
        joined = "\n".join(part for part in parts if part)
        return joined or opportunity.description or ""

    def _required_certificates(self, text: str) -> Inferred[list[str]] | None:
        # Nothing found means "unknown" (None), not "no certificates required" (3.3).
        found = self._certificates_from(text)
        if not found:
            return None
        return Inferred(value=found, provenance=Provenance.RULE, confidence=0.9)

    def _certificates_from(self, text: str) -> list[str]:
        found: list[str] = []
        rows_value = self._certificates.get("certificates", [])
        rows = cast(list[object], rows_value) if isinstance(rows_value, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = cast(dict[str, object], row)
            key = item.get("key")
            if not isinstance(key, str):
                continue
            patterns = item.get("patterns", [])
            pattern_values = cast(list[object], patterns) if isinstance(patterns, list) else []
            if any(
                isinstance(pattern, str)
                and re.search(rf"(?<!\w){re.escape(pattern)}(?!\w)", text, re.IGNORECASE)
                for pattern in pattern_values
            ):
                found.append(key)
        return found

    @staticmethod
    def _is_tenure(text: str, start: int, end: int) -> bool:
        window = text[max(0, start - _EXPERIENCE_WINDOW) : end + _EXPERIENCE_WINDOW]
        if not re.search(r"experience", window, re.IGNORECASE):
            return False
        # Contract length is written the same way as tenure and often shares a sentence
        # with the word "experience". Duration wording can sit on either side of the
        # number ("Contract Duration: 5 months", "3 Months duration possible"), so both
        # are checked. Dropping a genuine tenure that happens to sit next to a contract
        # length is the acceptable trade: a wrong experience level is worse than none.
        nearby = text[max(0, start - _DURATION_WINDOW) : end + _DURATION_WINDOW]
        return not re.search(_DURATION_WORDING, nearby, re.IGNORECASE)

    def _experience(self, text: str) -> Inferred[ExperienceLevel] | None:
        if re.search(r"\b(no|without) (prior )?experience\b", text, re.IGNORECASE):
            return Inferred(
                value=ExperienceLevel.ENTRY, provenance=Provenance.RULE, confidence=0.95
            )
        # A bare "N years" means nothing on its own. The same text also states
        # recertification cycles ("Food Hygiene course every 2 years") and contract
        # lengths ("Contract Duration: 5 months"), and both are routinely written
        # within a sentence of the word "experience". A match counts only when
        # "experience" is nearby AND no duration wording sits right before the number.
        # Maritime crewing states tenure in months as often as in years
        # ("Min 6 months in rank"), so both units are normalised to years.
        years = [
            int(match.group("count"))
            / (12 if match.group("unit").lower().startswith("month") else 1)
            for match in re.finditer(
                r"\b(?P<count>\d{1,2})\+? (?P<unit>years?|months?)\b", text, re.IGNORECASE
            )
            if self._is_tenure(text, match.start(), match.end())
        ]
        if not years:
            return None
        # max() over confirmed matches: a tenure requirement is a lower bound, so the
        # strictest one mentioned wins.
        maximum = max(years)
        level = (
            ExperienceLevel.SENIOR
            if maximum >= 5
            else ExperienceLevel.MID
            if maximum >= 2
            else ExperienceLevel.JUNIOR
        )
        return Inferred(value=level, provenance=Provenance.RULE, confidence=0.9)

    def _country(self, location: str | None) -> Inferred[str] | None:
        if not location:
            return None
        rows_value = self._countries.get("countries", [])
        rows = cast(list[object], rows_value) if isinstance(rows_value, list) else []
        folded = location.casefold()
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = cast(dict[str, object], row)
            code = item.get("code")
            if not isinstance(code, str):
                continue
            names = item.get("names", [])
            name_values = cast(list[object], names) if isinstance(names, list) else []
            if any(isinstance(name, str) and name.casefold() in folded for name in name_values):
                return Inferred(value=code, provenance=Provenance.RULE, confidence=0.9)
        return None
