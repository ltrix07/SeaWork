# ruff: noqa: I001 -- the pyright suppression on untyped langdetect must stay on its import.
import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import cast

import httpx
from bs4 import BeautifulSoup
from langdetect import DetectorFactory, LangDetectException, detect  # pyright: ignore[reportUnknownVariableType]

from seawork.domain.enums import SourceTier, SourceTrust
from seawork.domain.models import NormalizedOpportunity
from seawork.ingestion.base import RawItem
from seawork.ingestion.sources.base import BulkSource

DetectorFactory.seed = 0


def canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    # Block boundaries become newlines rather than spaces. These postings are mostly
    # <li> items with no terminating punctuation, so joining with a space produces one
    # run-on line — and every rule that reads a window around a match then reads into
    # the neighbouring item. That is how "…speak English." and a following
    # "Preferred: Degree from an accredited college" ended up in one sentence.
    text = BeautifulSoup(value, "html.parser").get_text("\n", strip=True)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line) or None


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, dict) else {}


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _description(posting: Mapping[str, object]) -> str | None:
    sections: list[str] = []
    for body_key, header_key in [
        ("description", None),
        ("key_responsibilities", "key_responsibilities_header"),
        ("skills_knowledge_expertise", "skills_knowledge_expertise_header"),
        ("benefits", "benefits_header"),
    ]:
        body = html_to_text(_optional_string(posting.get(body_key)))
        if not body:
            continue
        header = _optional_string(posting.get(header_key)) if header_key else None
        sections.append(f"{header}\n{body}" if header else body)
    return "\n\n".join(sections) or None


def _language(text: str | None) -> str | None:
    if not text:
        return None
    try:
        return cast(str, detect(text))
    except LangDetectException:
        return None


class PinpointSource(BulkSource):
    tier = SourceTier.OPEN

    def __init__(
        self,
        *,
        account: str,
        source_id: str,
        trust: SourceTrust,
        user_agent: str,
        timeout_seconds: float = 30.0,
        interval_seconds: float = 1.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.account = account
        self.source_id = source_id
        self.trust = trust
        super().__init__(
            base_url=f"https://{account}.pinpointhq.com",
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            client=client,
        )

    async def collect(self, since: datetime | None = None) -> AsyncIterator[RawItem]:
        del since  # Pinpoint provides no publication/update timestamp.
        await self._ensure_robots_allowed("/postings.json")
        response = await self._get(f"{self.base_url}/postings.json")
        document_value: object = response.json()
        if not isinstance(document_value, dict):
            raise ValueError("Pinpoint response must have a data array")
        document = cast(dict[str, object], document_value)
        if not isinstance(document.get("data"), list):
            raise ValueError("Pinpoint response must have a data array")
        data = cast(list[object], document["data"])
        fetched_at = datetime.now(UTC)
        for candidate in data:
            if not isinstance(candidate, dict):
                raise ValueError("Pinpoint posting must be an object")
            posting = cast(dict[str, object], candidate)
            external_id = str(posting.get("id", ""))
            url = posting.get("url")
            if not external_id or not isinstance(url, str):
                raise ValueError("Pinpoint posting is missing id or url")
            payload = canonical_json(posting)
            yield RawItem.model_validate(
                {
                    "source_id": self.source_id,
                    "external_id": external_id,
                    "url": url,
                    "fetched_at": fetched_at,
                    "payload": payload,
                    "content_type": "application/json",
                    "content_hash": hashlib.sha256(payload.encode()).hexdigest(),
                    "http_status": response.status_code,
                }
            )

    def normalize(self, item: RawItem) -> NormalizedOpportunity:
        value: object = json.loads(item.payload)
        if not isinstance(value, dict):
            raise ValueError("Pinpoint raw payload must be an object")
        posting = cast(dict[str, object], value)
        job = _mapping(posting.get("job"))
        employer = _mapping(job.get("structure_custom_group_one"))
        location = _mapping(posting.get("location"))
        description = _description(posting)
        return NormalizedOpportunity(
            source_id=item.source_id,
            external_id=item.external_id,
            url=item.url,
            title=str(posting.get("title", "")).strip(),
            description=description,
            employer=_optional_string(employer.get("name")),
            location_raw=None,
            recruitment_office_raw=_optional_string(location.get("name")),
            salary_raw=_optional_string(posting.get("compensation")),
            posted_at=None,
            declared_type=_optional_string(posting.get("employment_type")),
            language=_language(description),
            source_fields={
                "department": _optional_string(_mapping(job.get("department")).get("name")),
                "division": _optional_string(_mapping(job.get("division")).get("name")),
                "skills_knowledge_expertise": html_to_text(
                    _optional_string(posting.get("skills_knowledge_expertise"))
                ),
                # Despite the field name, "benefits" is where these employers put the
                # "Travel Requirements" block: passport validity, seaman book, C1/D
                # visa, marine medical, Marlins score. Those are hiring requirements,
                # not benefits, and skipping the field cost 38 of 93 records their
                # certificates. Trust the content, not the label the ATS gives it.
                "additional_requirements": html_to_text(_optional_string(posting.get("benefits"))),
            },
        )
