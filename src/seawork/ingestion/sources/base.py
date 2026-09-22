import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from seawork.domain.enums import SourceTier, SourceTrust
from seawork.domain.models import NormalizedOpportunity
from seawork.ingestion.base import RawItem


class RetryableHttpError(RuntimeError):
    pass


class BaseHttpSource(ABC):
    source_id: str
    tier: SourceTier
    trust: SourceTrust

    def __init__(
        self,
        *,
        base_url: str,
        user_agent: str,
        timeout_seconds: float = 30.0,
        interval_seconds: float = 1.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url
        self.user_agent = user_agent
        self._interval = interval_seconds
        self._last_request = 0.0
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds, headers={"User-Agent": user_agent}, follow_redirects=True
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _rate_limit(self) -> None:
        remaining = self._interval - (time.monotonic() - self._last_request)
        if remaining > 0:
            await asyncio.sleep(remaining)

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, RetryableHttpError)),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def _get(self, url: str) -> httpx.Response:
        await self._rate_limit()
        try:
            response = await self._client.get(url)
        finally:
            self._last_request = time.monotonic()
        if response.status_code >= 500:
            raise RetryableHttpError(f"Server returned {response.status_code} for {url}")
        response.raise_for_status()
        return response

    async def _ensure_robots_allowed(self, path: str) -> None:
        robots_url = urljoin(self.base_url, "/robots.txt")
        try:
            response = await self._get(robots_url)
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {404, 410}:
                return
            raise
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines())
        if not parser.can_fetch(self.user_agent, urljoin(self.base_url, path)):
            raise PermissionError(f"robots.txt forbids collection of {path}")

    @abstractmethod
    def normalize(self, item: RawItem) -> NormalizedOpportunity: ...

    def quality_notes(self, opportunity: NormalizedOpportunity) -> tuple[str, ...]:
        """Facts about a record that must be kept without rejecting it.

        Default empty: most sources have nothing to say here. An adapter overrides
        it when the source tells us something about its own record that the shared
        quality check cannot know - see PadiSource and its trashed postings.
        """
        return ()


class BulkSource(BaseHttpSource, ABC):
    @abstractmethod
    def collect(self, since: datetime | None = None) -> AsyncIterator[RawItem]:
        raise NotImplementedError


class ListDetailSource(BaseHttpSource, ABC):
    @abstractmethod
    def collect(self, since: datetime | None = None) -> AsyncIterator[RawItem]:
        raise NotImplementedError
