from dataclasses import dataclass
from urllib.parse import urlparse

from seawork.domain.models import NormalizedOpportunity


@dataclass(frozen=True)
class QualityResult:
    score: float
    flags: list[str]

    @property
    def accepted(self) -> bool:
        return not self.flags


def check_quality(
    opportunity: NormalizedOpportunity, *, duplicate_content: bool = False
) -> QualityResult:
    flags: list[str] = []
    title = opportunity.title.strip()
    if not title or title.lower() in {"untitled", "n/a", "test", "-"}:
        flags.append("empty_title")
    parsed_url = urlparse(str(opportunity.url))
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        flags.append("invalid_url")
    if not opportunity.description or len(opportunity.description.strip()) < 100:
        flags.append("description_too_short")
    if duplicate_content:
        flags.append("duplicate_content")
    return QualityResult(score=max(0.0, 1.0 - 0.25 * len(flags)), flags=flags)
