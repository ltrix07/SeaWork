from dataclasses import dataclass
from pathlib import Path
from typing import cast

from seawork.domain.enums import OpportunityType
from seawork.domain.models import NormalizedOpportunity
from seawork.ingestion.references import load_yaml


@dataclass(frozen=True)
class Classification:
    type: OpportunityType
    confidence: float


def classify(opportunity: NormalizedOpportunity, rules_path: Path) -> Classification:
    rules = load_yaml(rules_path)
    declared_value = rules.get("declared_types", {})
    declared = cast(dict[str, object], declared_value) if isinstance(declared_value, dict) else {}
    if opportunity.declared_type:
        mapped = declared.get(opportunity.declared_type.lower())
        if isinstance(mapped, str):
            return Classification(OpportunityType(mapped), 1.0)

    keywords_value = rules.get("keywords", {})
    keywords = (
        cast(dict[object, object], keywords_value) if isinstance(keywords_value, dict) else {}
    )
    title = opportunity.title.casefold()
    for keyword, mapped in keywords.items():
        if isinstance(keyword, str) and isinstance(mapped, str) and keyword.casefold() in title:
            return Classification(OpportunityType(mapped), 0.8)
    return Classification(OpportunityType.JOB, 0.5)
