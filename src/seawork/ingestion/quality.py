from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import urlparse

from seawork.domain.models import NormalizedOpportunity


def _no_notes() -> list[str]:
    return []


@dataclass(frozen=True)
class QualityResult:
    """Two lists, because they answer different questions.

    `flags` are reasons to reject: an empty title, an unusable URL. Anything here
    stops the record from entering the corpus, which is why `accepted` reads it.

    `notes` are facts worth keeping about a record we do accept - a posting the
    source has trashed in its own CMS but still serves, for instance. Before this
    split there was nowhere to put such a fact: adding it to `flags` rejected the
    record, and leaving it out lost it. Sources were writing it into source_fields
    instead, where nothing looks for it.
    """

    score: float
    flags: list[str]
    notes: list[str] = field(default_factory=_no_notes)

    @property
    def accepted(self) -> bool:
        return not self.flags


def check_quality(
    opportunity: NormalizedOpportunity,
    *,
    duplicate_content: bool = False,
    notes: Sequence[str] = (),
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
    # Notes do not move the score. A record is not lower quality for carrying a fact
    # about where it came from; it is lower quality for missing a title.
    return QualityResult(score=max(0.0, 1.0 - 0.25 * len(flags)), flags=flags, notes=list(notes))
