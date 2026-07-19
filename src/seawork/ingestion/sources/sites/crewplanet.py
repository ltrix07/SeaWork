# ruff: noqa: I001 -- the pyright suppression on untyped langdetect must stay on its import.
import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import cast

import httpx
from bs4 import BeautifulSoup
from langdetect import DetectorFactory, LangDetectException, detect  # pyright: ignore[reportUnknownVariableType]

from seawork.domain.enums import SourceTier, SourceTrust
from seawork.domain.models import NormalizedOpportunity
from seawork.ingestion.base import RawItem
from seawork.ingestion.sources.base import BulkSource

FEED_PATH = "/v2/en/vacancies/feed/rss"
DetectorFactory.seed = 0


def _required_text(item: ET.Element, name: str) -> str:
    value = item.findtext(name)
    if value is None or not value.strip():
        raise ValueError(f"Crewplanet item is missing {name}")
    return value.strip()


def _language(text: str | None) -> str | None:
    if not text:
        return None
    try:
        return cast(str, detect(text))
    except LangDetectException:
        return None


def _without_terminal_period(value: str) -> str:
    return value[:-1].rstrip() if value.endswith(".") else value


def _description_fields(html: str) -> tuple[str | None, str, str, str]:
    text = BeautifulSoup(html, "html.parser").get_text("\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines[:-2]):
        if not line.startswith("Salary:"):
            continue
        if not lines[index + 1].startswith("Join date:"):
            continue
        if not lines[index + 2].startswith("Contract duration:"):
            continue
        salary = _without_terminal_period(line.removeprefix("Salary:").strip())
        join_date = _without_terminal_period(
            lines[index + 1].removeprefix("Join date:").strip()
        )
        duration = _without_terminal_period(
            lines[index + 2].removeprefix("Contract duration:").strip()
        )
        description = "\n".join(lines[:index]).strip() or None
        return description, salary, join_date, duration
    raise ValueError("Crewplanet description has no structured vacancy tail")


def _field_from_description(description: str | None, label: str) -> str | None:
    if not description:
        return None
    match = re.search(rf"(?im)^{re.escape(label)}:\s*(.+)$", description)
    return match.group(1).strip() if match else None


def _requirements(description: str | None) -> str | None:
    if not description:
        return None
    lines = description.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"Requirements:\s*(.*)", line, re.IGNORECASE)
        if match is None:
            continue
        result = [match.group(1)] if match.group(1) else []
        for candidate in lines[index + 1 :]:
            if re.fullmatch(r"[A-Za-z][^:]{0,60}:\s*", candidate):
                break
            result.append(candidate)
        value = "\n".join(result).strip()
        return value or None
    return None


class CrewplanetSource(BulkSource):
    tier = SourceTier.OPEN

    def __init__(
        self,
        *,
        source_id: str,
        trust: SourceTrust,
        user_agent: str,
        timeout_seconds: float = 30.0,
        interval_seconds: float = 1.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.source_id = source_id
        self.trust = trust
        super().__init__(
            base_url="https://www.crewplanet.eu",
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            client=client,
        )

    async def collect(self, since: datetime | None = None) -> AsyncIterator[RawItem]:
        del since  # The feed always returns the complete current snapshot.
        await self._ensure_robots_allowed(FEED_PATH)
        response = await self._get(f"{self.base_url}{FEED_PATH}")
        root = ET.fromstring(response.content)
        fetched_at = datetime.now(UTC)
        for item in root.findall("./channel/item"):
            guid = _required_text(item, "guid")
            match = re.search(r"/vacId/(\d+)(?:/)?$", guid)
            if match is None:
                raise ValueError(f"Crewplanet guid has no vacId: {guid}")
            payload = ET.tostring(item, encoding="unicode", short_empty_elements=True)
            yield RawItem.model_validate(
                {
                    "source_id": self.source_id,
                    "external_id": match.group(1),
                    "url": _required_text(item, "link"),
                    "fetched_at": fetched_at,
                    "payload": payload,
                    "content_type": "text/xml",
                    "content_hash": hashlib.sha256(payload.encode()).hexdigest(),
                    "http_status": response.status_code,
                }
            )

    def normalize(self, item: RawItem) -> NormalizedOpportunity:
        element = ET.fromstring(item.payload)
        description, salary, join_date, duration = _description_fields(
            _required_text(element, "description")
        )
        posted_at = parsedate_to_datetime(_required_text(element, "pubDate"))
        return NormalizedOpportunity(
            source_id=item.source_id,
            external_id=item.external_id,
            url=item.url,
            title=_required_text(element, "title"),
            description=description,
            employer=None,
            location_raw=_field_from_description(description, "Location"),
            salary_raw=salary,
            posted_at=posted_at,
            declared_type="job",
            language=_language(description),
            source_fields={
                "join_date": join_date,
                "contract_duration": duration,
                "skills_knowledge_expertise": _requirements(description),
            },
        )
