from pydantic import BaseModel, ConfigDict, Field, model_validator

from seawork.domain.enums import Provenance


class Inferred[T](BaseModel):
    model_config = ConfigDict(frozen=True)

    value: T
    provenance: Provenance
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def source_is_certain(self) -> "Inferred[T]":
        if self.provenance is Provenance.SOURCE and self.confidence != 1.0:
            raise ValueError("SOURCE provenance must have confidence=1.0")
        return self
