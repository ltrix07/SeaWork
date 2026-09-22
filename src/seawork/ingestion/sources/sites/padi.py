# ruff: noqa: I001 -- the pyright suppression on untyped langdetect must stay on its import.
import hashlib
import html
import re
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import cast

import httpx
from langdetect import DetectorFactory, LangDetectException, detect  # pyright: ignore[reportUnknownVariableType]

from seawork.domain.enums import SourceTier, SourceTrust
from seawork.domain.models import NormalizedOpportunity
from seawork.ingestion.base import RawItem
from seawork.ingestion.sources.base import BulkSource
from seawork.ingestion.sources.platforms.pinpoint import html_to_text

DetectorFactory.seed = 0

# job_per_page is a theme shortcode attribute, not a documented API parameter (source
# contract §1). Without it the feed silently falls back to 10 items, so the value is a
# constructor default rather than a URL literal: the day the board outgrows it, raising
# the default is the fix, not hunting for a hardcoded string.
DEFAULT_JOB_PER_PAGE = 300

# The prefix is a leftover from a retired numbering scheme, not a priority or a code
# (source contract §3.1). Stripping it here, rather than aliasing "1. Teaching /
# Guiding" in professions.yaml, means a future renumbering cannot silently invalidate
# the reference file.
_SECTOR_PREFIX = re.compile(r"^\d+\.\s*")


def _required_text(item: ET.Element, name: str) -> str:
    # PADI's identifier field is spelled RecuiterJobNumber -- missing the first "r" of
    # "Recruiter" -- on the source's side (§3.2). Read the name exactly as given: fixing
    # the typo here would mean the day PADI corrects it, this line keeps matching the
    # old spelling and starts silently losing every external_id instead of failing loudly.
    value = item.findtext(name)
    if value is None or not value.strip():
        raise ValueError(f"PADI item is missing {name}")
    return html.unescape(value.strip())


def _optional_text(item: ET.Element, name: str) -> str | None:
    value = item.findtext(name)
    if value is None or not value.strip():
        return None
    return html.unescape(value.strip())


def _language(text: str | None) -> str | None:
    if not text:
        return None
    try:
        return cast(str, detect(text))
    except LangDetectException:
        return None


def _location_raw(item: ET.Element) -> str | None:
    """Keep the whole address, only dropping the source's own "NA" placeholder.

    location is a free-form dive-centre address of one to eight comma-separated
    segments (§3.4): street, shop name, postcode, sometimes the country repeated.
    Only the first segment is reliably the country, but splitting the rest apart is
    guesswork the source never asked for, so everything after it is kept verbatim.
    Two records end their address with a literal ", NA" segment; that placeholder
    is dropped, the rest of the address is not.
    """
    value = _optional_text(item, "location")
    if value is None:
        return None
    segments = [segment.strip() for segment in value.split(",")]
    if segments and segments[-1].casefold() == "na":
        segments = segments[:-1]
    cleaned = ", ".join(segment for segment in segments if segment)
    return cleaned or None


class PadiSource(BulkSource):
    tier = SourceTier.OPEN

    def __init__(
        self,
        *,
        source_id: str,
        trust: SourceTrust,
        user_agent: str,
        timeout_seconds: float = 30.0,
        interval_seconds: float = 1.0,
        job_per_page: int = DEFAULT_JOB_PER_PAGE,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.source_id = source_id
        self.trust = trust
        self._job_per_page = job_per_page
        super().__init__(
            base_url="https://divejobs.padi.com",
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            client=client,
        )

    async def collect(self, since: datetime | None = None) -> AsyncIterator[RawItem]:
        del since  # The feed always returns the complete current snapshot; there is no paging (§1).
        path = f"/?feed=job_feed&sh_atts=job_per_page:{self._job_per_page}"
        await self._ensure_robots_allowed(path)
        response = await self._get(f"{self.base_url}{path}")
        root = ET.fromstring(response.content)
        fetched_at = datetime.now(UTC)
        for item in root.findall("./channel/item"):
            external_id = _required_text(item, "RecuiterJobNumber")
            payload = ET.tostring(item, encoding="unicode", short_empty_elements=True)
            yield RawItem.model_validate(
                {
                    "source_id": self.source_id,
                    "external_id": external_id,
                    "url": _required_text(item, "link"),
                    "fetched_at": fetched_at,
                    "payload": payload,
                    "content_type": "text/xml",
                    "content_hash": hashlib.sha256(payload.encode()).hexdigest(),
                    "http_status": response.status_code,
                }
            )

    def quality_notes(self, opportunity: NormalizedOpportunity) -> tuple[str, ...]:
        """Mark the postings WordPress has trashed but the feed still serves (§3.3).

        The record stays ordinary: it has a date, a description and an employer, and
        `__trashed` describes the state of a post in someone else's CMS, not whether
        the vacancy is worth keeping. Until quality gained notes there was nowhere to
        say this -- a flag would have rejected all 17 of them.
        """
        return ("source_trashed",) if "__trashed" in str(opportunity.url) else ()

    def normalize(self, item: RawItem) -> NormalizedOpportunity:
        element = ET.fromstring(item.payload)
        description = html_to_text(_required_text(element, "description"))
        posted_at = parsedate_to_datetime(_required_text(element, "PostDate"))
        sector = _required_text(element, "sector")
        return NormalizedOpportunity(
            source_id=item.source_id,
            external_id=item.external_id,
            url=item.url,
            title=_required_text(element, "title"),
            description=description,
            employer=_required_text(element, "employer"),
            location_raw=_location_raw(element),
            # recruitment_office_raw is left unset: PADI has no separate hiring office,
            # the dive centre in `employer` is the one doing the hiring (§4.2).
            salary_raw=_optional_text(element, "salary"),
            posted_at=posted_at,
            declared_type=_optional_text(element, "type"),
            language=_language(description),
            source_fields={
                "expiry_date": _required_text(element, "expiryDate"),
                # Named after the source's own field, not renamed to "department": the
                # generic profession matcher (RulesEnricher.enrich) only reads
                # "department"/"division", so this text currently reaches profession
                # matching through neither key -- title is the only signal that does.
                # Left as-is rather than silently retargeted, since fixing that gap is
                # a decision about shared enrichment code, out of scope here.
                "sector": _SECTOR_PREFIX.sub("", sector),
                # "no" for all 282 records at recon time; kept in case the board starts
                # using it (§4.3).
                "featured": _required_text(element, "featured"),
            },
        )
