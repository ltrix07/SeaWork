"""Persistent cache for paid LLM enrichment responses."""

from sqlalchemy.orm import Session

from seawork.storage.models import LlmCacheRecord


class SqlLLMCache:
    """Portable SQL implementation of the LLM cache protocol."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, key: tuple[str, str, str]) -> dict[str, object] | None:
        record = self._session.get(LlmCacheRecord, key)
        return record.response if record is not None else None

    def put(self, key: tuple[str, str, str], value: dict[str, object]) -> None:
        content_hash, prompt_version, model_id = key
        self._session.merge(
            LlmCacheRecord(
                content_hash=content_hash,
                prompt_version=prompt_version,
                model_id=model_id,
                response=value,
            )
        )
        self._session.commit()
