"""Keys a profile may hold, read from the same files the vacancy side uses.

Two different checks, on purpose. Professions, directions, certificates and languages
are keys we invented, so a typo in one is our bug and is caught against the files.
Country codes are an external standard (ISO 3166-1 alpha-2) that we do not maintain,
so only their shape is checked: a well-formed but non-existent code matches nothing
and deceives nobody, and checking completeness would pretend we own the standard.
countries.yaml is not used here at all - it is a text-parsing dictionary that grows
with the sources, not a list of countries.

A profession in a profile and a profession in a vacancy must be one key from
professions.yaml, otherwise matching degrades into comparing strings (contract 4).
Checking at write time is the only place where the mistake is still traceable to the
caller that made it.
"""

import re
from pathlib import Path
from typing import Any, cast

from seawork.domain.profile import CareerGoal
from seawork.ingestion.references import load_yaml


class UnknownReferenceKeyError(ValueError):
    def __init__(self, field: str, unknown: list[str]) -> None:
        super().__init__(f"{field}: not in the reference vocabulary: {unknown}")
        self.field = field
        self.unknown = unknown


_COUNTRY_CODE = re.compile(r"[A-Z]{2}")

# Profile field -> vocabulary name; "country_code" means shape check only.
# Languages are included because languages.yaml states it is shared with the profile.
_FIELD_VOCABULARY = {
    "professions": "professions",
    "directions": "directions",
    "certificates": "certificates",
    "languages": "languages",
    "residence_country": "country_code",
    "citizenship": "country_code",
    "preferred_countries": "country_code",
    "hiring_country": "country_code",
    "work_authorization": "country_code",
}


class ProfileVocabulary:
    def __init__(self, keys: dict[str, frozenset[str]]) -> None:
        self._keys = keys

    @classmethod
    def from_directory(cls, reference_dir: Path) -> "ProfileVocabulary":
        """Read the reference files once; the repository keeps the result."""

        def keys_of(filename: str, section: str, key: str) -> frozenset[str]:
            rows: list[dict[str, Any]] = load_yaml(reference_dir / filename).get(section, [])
            return frozenset(str(row[key]) for row in rows)

        return cls(
            {
                "professions": keys_of("professions.yaml", "professions", "key"),
                "directions": keys_of("directions.yaml", "directions", "key"),
                "certificates": keys_of("certificates.yaml", "certificates", "key"),
                "languages": keys_of("languages.yaml", "languages", "key"),
            }
        )

    def check(self, field: str, value: object) -> None:
        """Raise `UnknownReferenceKeyError` if the value holds a key we do not know."""
        if field == "career_goal" and isinstance(value, CareerGoal):
            for goal_field, vocabulary in (
                ("profession", "professions"),
                ("direction", "directions"),
                ("country", "country_code"),
            ):
                self._check_keys(
                    f"career_goal.{goal_field}", vocabulary, getattr(value, goal_field)
                )
            return
        vocabulary = _FIELD_VOCABULARY.get(field)
        if vocabulary is not None:
            self._check_keys(field, vocabulary, value)

    def canonicalize(self, field: str, value: object) -> object:
        """Upper-case country codes before they are checked and stored.

        Canonicalising is not inferring. An alpha-2 code has exactly one upper-case
        form, so "pl" -> "PL" invents nothing and loses nothing; refusing it would
        make the boundary brittle over a mistake with a single unambiguous fix. That
        is the line: reading a bare "$" as USD (parse_salary) *is* inference, and
        carries reduced confidence for it. Case is not.
        """
        if _FIELD_VOCABULARY.get(field) != "country_code":
            return value
        if isinstance(value, str):
            return value.upper()
        if isinstance(value, list):
            return [
                item.upper() if isinstance(item, str) else item
                for item in cast(list[object], value)
            ]
        return value

    def _check_keys(self, field: str, vocabulary: str, value: object) -> None:
        if value is None:
            return
        candidates: list[object] = (
            list(cast(list[object], value)) if isinstance(value, list) else [value]
        )
        if vocabulary == "country_code":
            unknown = [
                str(item)
                for item in candidates
                if not isinstance(item, str) or not _COUNTRY_CODE.fullmatch(item)
            ]
        else:
            unknown = [str(item) for item in candidates if item not in self._keys[vocabulary]]
        if unknown:
            raise UnknownReferenceKeyError(field, unknown)
