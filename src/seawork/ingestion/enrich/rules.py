import re
from pathlib import Path
from typing import cast

from seawork.domain.enums import ExperienceLevel, Provenance
from seawork.domain.inferred import Inferred
from seawork.domain.models import EnrichedOpportunity, NormalizedOpportunity
from seawork.ingestion.classify import Classification
from seawork.ingestion.quality import QualityResult
from seawork.ingestion.references import load_yaml

# Окно поиска слова "experience" вокруг совпадения "N years", в символах.
_EXPERIENCE_WINDOW = 60


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
        searchable = " ".join(
            part
            for part in [
                opportunity.source_fields.get("department"),
                opportunity.source_fields.get("division"),
            ]
            if part
        )
        qualification_text = opportunity.source_fields.get("skills_knowledge_expertise") or ""
        return EnrichedOpportunity(
            normalized=opportunity,
            type=classification.type,
            type_confidence=classification.confidence,
            country=self._country(opportunity.location_raw),
            city=None,
            profession=self._profession(searchable),
            direction=(
                Inferred(value=self._direction, provenance=Provenance.RULE, confidence=1.0)
                if self._direction
                else None
            ),
            required_certificates=self._required_certificates(qualification_text),
            experience_level=self._experience(qualification_text),
            salary=None,
            workplace_type_hint=(
                Inferred(value=self._workplace, provenance=Provenance.RULE, confidence=1.0)
                if self._workplace
                else None
            ),
            quality_score=quality.score,
            quality_flags=quality.flags,
        )

    def _profession(self, text: str) -> Inferred[str] | None:
        folded = text.casefold()
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
            if any(isinstance(alias, str) and alias.casefold() in folded for alias in alias_values):
                return Inferred(value=key, provenance=Provenance.RULE, confidence=0.9)
        return None

    def _required_certificates(self, text: str) -> Inferred[list[str]] | None:
        # Ничего не нашли — это "неизвестно" (None), а не "сертификаты не нужны" (§3.3).
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

    def _experience(self, text: str) -> Inferred[ExperienceLevel] | None:
        if re.search(r"\b(no|without) (prior )?experience\b", text, re.IGNORECASE):
            return Inferred(
                value=ExperienceLevel.ENTRY, provenance=Provenance.RULE, confidence=0.95
            )
        # "N years" само по себе ничего не значит: в тексте квалификаций так же
        # записана периодичность переаттестации ("Food Hygiene course every 2 years").
        # Засчитываем только совпадения, рядом с которыми есть слово experience.
        years = [
            int(match.group(1))
            for match in re.finditer(r"\b(\d{1,2})\+? years?\b", text, re.IGNORECASE)
            if re.search(
                r"experience",
                text[max(0, match.start() - _EXPERIENCE_WINDOW) : match.end() + _EXPERIENCE_WINDOW],
                re.IGNORECASE,
            )
        ]
        if not years:
            return None
        # max() по подтверждённым совпадениям: требование стажа — это нижняя граница,
        # и из нескольких упомянутых берётся самое строгое.
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
