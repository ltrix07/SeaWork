"""Conservative LLM completion for the two fields rules cannot determine."""
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportArgumentType=false

import asyncio
import hashlib
import logging
import re
import threading
from collections.abc import Awaitable
from pathlib import Path
from typing import Protocol

from seawork.domain.enums import ExperienceLevel, Provenance
from seawork.domain.inferred import Inferred
from seawork.domain.models import EnrichedOpportunity, NormalizedOpportunity
from seawork.ingestion.classify import Classification
from seawork.ingestion.enrich.rules import RulesEnricher
from seawork.ingestion.quality import QualityResult
from seawork.ingestion.references import load_yaml

PROMPT_VERSION = "5"
_LOG = logging.getLogger(__name__)
_MEANINGFUL_TEXT = re.compile(r"\S")


class LLMClient(Protocol):
    """A provider adapter. Implementations must use constrained JSON output."""

    model_id: str

    async def extract(self, prompt: str, schema: dict[str, object]) -> dict[str, object]: ...


class LLMCache(Protocol):
    def get(self, key: tuple[str, str, str]) -> dict[str, object] | None: ...

    def put(self, key: tuple[str, str, str], value: dict[str, object]) -> None: ...


class MemoryLLMCache:
    def __init__(self) -> None:
        self._values: dict[tuple[str, str, str], dict[str, object]] = {}

    def get(self, key: tuple[str, str, str]) -> dict[str, object] | None:
        return self._values.get(key)

    def put(self, key: tuple[str, str, str], value: dict[str, object]) -> None:
        self._values[key] = value


def normalise_whitespace(text: str) -> str:
    """Use the same deliberately narrow normalization as the gold-set checker."""
    for old, new in (("\u2019", "'"), ("\u2018", "'"), ("\u201c", '"'), ("\u201d", '"')):
        text = text.replace(old, new)
    return " ".join(text.replace("\u00a0", " ").split())


def response_schema() -> dict[str, object]:
    basis = {"type": "string", "enum": ["stated", "inferred"]}
    quote = {"type": "string"}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["experience_level", "required_certificates"],
        "properties": {
            "experience_level": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["value", "basis", "quote"],
                        "properties": {
                            "value": {
                                "type": "string",
                                "enum": [item.value for item in ExperienceLevel],
                            },
                            "basis": basis,
                            "quote": quote,
                        },
                    },
                ]
            },
            "required_certificates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["key", "basis", "quote"],
                    "properties": {"key": {"type": "string"}, "basis": basis, "quote": quote},
                },
            },
        },
    }


def build_prompt(text: str, certificate_keys: list[str]) -> str:
    return f"""You extract two hiring requirements from a maritime vacancy text:
  experience_level and required_certificates. Return only the supplied structured
  schema and extract nothing else. Every non-null result must include, as its quote,
  an exact verbatim substring copied from the input text.

  EXPERIENCE LEVEL
  If the text states no experience requirement, return experience_level as null.
  This is normal and common — do not guess.
  A stated number of years decides the level:
    - no experience required, or trainee / entry-level  -> entry
    - less than 2 years                                 -> junior
    - 2 to 4 years                                      -> mid
    - 5 years or more                                   -> senior
  A number always wins, even next to leadership wording ('Minimum 3 years
  leadership experience' -> mid). Use lead only when prior leadership experience is
  required AND no number is given ('proven experience leading large teams'). When
  experience is required but no number is given ('previous experience in rank'),
  choose junior, the lower bound.
  Set basis to stated when a number or an explicit requirement is present, and to
  inferred when the requirement is only implied ('experience in rank on similar
  vessel type'). Do not derive experience from contract duration, recertification
  frequency, or vessel type ('Contract Duration: 5 months', 'Food Hygiene course
  every 2 years' are not experience). Duties and skills the job involves are not
  experience requirements.

  REQUIRED CERTIFICATES
  Certificate keys may only be: {", ".join(certificate_keys)}.
  Include a certificate only when the candidate must already hold it ('Must hold a
  valid STCW certificate'). This is the whole test — apply it strictly:
  - If a certificate is described as recommended, strongly recommended, desirable,
    preferred, an advantage, or a plus, it is NOT required — omit it. This holds for
    every certificate, including STCW and other safety certificates ('an STCW Ship's
    Cook Certificate is recommended' -> omit).
  - Knowledge of, understanding of, familiarity with, or working in accordance with a
    standard or regulation ('knowledge of USPH standards', 'understanding of IMO and
    STCW regulations') describes how the work is done, not a document held — omit it.
  - A recertification interval ('Food Hygiene course every 2 years') or a passing
    mention of a topic is not a held certificate — omit it.
  - Use a key only when the text names that specific certificate. If a required
    certificate has no matching key in the list above, omit it — never substitute the
    nearest key (a 'First Aid / BLS certificate' is not a marine medical certificate).
  - Documents of one family are not interchangeable. An identity or travel document
    key applies only when the text names that exact document: a visa issued by one
    country is not a visa issued by another, and a document that is merely implied by
    another one is not stated. Omit rather than pick the closest relative.
  - A standard, programme or regulation can also be a certificate someone holds, so
    decide by how the text refers to it, not by whether it is named. Use the key only
    where the text requires the candidate to hold, possess, or be certified in it.
    Naming it among the systems, rules or programmes the operation follows is not
    enough — this holds even when no word like 'knowledge of' precedes it.
  If no certificate must be held, return an empty list. Do not let these rules push
  you into dropping a certificate the text plainly requires: every rule above narrows
  what counts as required, none of them lowers the value of a genuine requirement.


  Input text:
  {text}"""


def _run(coro: Awaitable[dict[str, object]]) -> dict[str, object]:
    """Run an async adapter from the existing synchronous enrichment boundary."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    result: dict[str, object] | None = None
    error: BaseException | None = None

    def runner() -> None:
        nonlocal result, error
        try:
            result = asyncio.run(coro)
        except BaseException as exc:  # propagated below, preserving degradation path
            error = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if error is not None:
        raise error
    if result is None:
        raise RuntimeError("LLM adapter returned no result")
    return result


class LLMEnricher:
    """Rules plus a fail-open LLM pass for still-unknown experience/certificates."""

    def __init__(
        self,
        rules: RulesEnricher,
        client: LLMClient | None,
        reference_dir: Path,
        *,
        cache: LLMCache | None = None,
    ) -> None:
        self._rules = rules
        self._client = client
        self._cache = cache or MemoryLLMCache()
        data = load_yaml(reference_dir / "certificates.yaml")
        rows = data.get("certificates", [])
        self._certificate_keys = [
            row["key"] for row in rows if isinstance(row, dict) and isinstance(row.get("key"), str)
        ]

    def enrich(
        self,
        opportunity: NormalizedOpportunity,
        classification: Classification,
        quality: QualityResult,
    ) -> EnrichedOpportunity:
        enriched = self._rules.enrich(opportunity, classification, quality)
        if self._client is None or (
            enriched.experience_level is not None and enriched.required_certificates is not None
        ):
            return enriched
        text = RulesEnricher.qualification_text(opportunity)
        if len(_MEANINGFUL_TEXT.findall(normalise_whitespace(text))) < 20:
            return enriched
        key = (hashlib.sha256(text.encode()).hexdigest(), PROMPT_VERSION, self._client.model_id)
        response = self._cache.get(key)
        if response is None:
            try:
                response = _run(
                    self._client.extract(
                        build_prompt(text, self._certificate_keys), response_schema()
                    )
                )
                self._cache.put(key, response)
            except Exception:
                _LOG.warning("llm_unavailable", exc_info=True)
                return enriched
        return self._merge(enriched, response, text)

    @staticmethod
    def _inferred(value: object, basis: object) -> Inferred[object] | None:
        if basis not in {"stated", "inferred"}:
            return None
        confidence = 0.9 if basis == "stated" else 0.6
        return Inferred(value=value, provenance=Provenance.LLM, confidence=confidence)

    def _merge(
        self, enriched: EnrichedOpportunity, response: dict[str, object], source_text: str
    ) -> EnrichedOpportunity:
        # model_copy preserves every rule/source field exactly; LLM only fills None.
        updates: dict[str, object] = {}
        if enriched.experience_level is None:
            item = response.get("experience_level")
            if isinstance(item, dict):
                value, quote = item.get("value"), item.get("quote")
                if (
                    not isinstance(value, str)
                    or not isinstance(quote, str)
                    or normalise_whitespace(quote) not in normalise_whitespace(source_text)
                ):
                    _LOG.warning(
                        "llm_quote_not_found" if isinstance(quote, str) else "llm_invalid_response"
                    )
                else:
                    try:
                        level = ExperienceLevel(value)
                    except ValueError:
                        _LOG.warning("llm_invalid_experience_level", extra={"value": value})
                    else:
                        inferred = self._inferred(level, item.get("basis"))
                        if inferred is not None:
                            updates["experience_level"] = inferred
        if enriched.required_certificates is None:
            raw_certs = response.get("required_certificates")
            if isinstance(raw_certs, list):
                values: list[str] = []
                basis: object | None = None
                for item in raw_certs:
                    if not isinstance(item, dict):
                        continue
                    cert, quote, item_basis = item.get("key"), item.get("quote"), item.get("basis")
                    if not isinstance(cert, str) or cert not in self._certificate_keys:
                        _LOG.warning("llm_invalid_certificate", extra={"key": cert})
                        continue
                    if not isinstance(quote, str) or normalise_whitespace(
                        quote
                    ) not in normalise_whitespace(source_text):
                        _LOG.warning("llm_quote_not_found", extra={"key": cert})
                        continue
                    if item_basis not in {"stated", "inferred"}:
                        continue
                    if cert not in values:
                        values.append(cert)
                    # Certificates are normally stated; the conservative value wins if mixed.
                    basis = "inferred" if item_basis == "inferred" else basis or "stated"
                if values and basis is not None:
                    inferred = self._inferred(values, basis)
                    if inferred is not None:
                        updates["required_certificates"] = inferred
        return enriched.model_copy(update=updates)
