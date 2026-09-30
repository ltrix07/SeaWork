from pydantic import BaseModel, ConfigDict, Field, model_validator

from seawork.domain.enums import Provenance


class Inferred[T](BaseModel):
    model_config = ConfigDict(frozen=True)

    value: T
    provenance: Provenance
    confidence: float = Field(ge=0.0, le=1.0)
    # The words the value was read from, keyed by the part of the value they support:
    # {"mid": "Minimum 3 years of experience"} for a scalar, one entry per key for a
    # list. Optional, because a value derived from a structured source field has no
    # sentence behind it.
    #
    # The LLM layer verified these quotes against the source text and then dropped
    # them, which left explainability (SPEC.md 10.7) with a key and a provenance and
    # nothing to show a person. Verifying a quote and discarding it is half a feature.
    quotes: dict[str, str] | None = None

    @model_validator(mode="after")
    def source_is_certain(self) -> "Inferred[T]":
        if self.provenance is Provenance.SOURCE and self.confidence != 1.0:
            raise ValueError("SOURCE provenance must have confidence=1.0")
        return self
