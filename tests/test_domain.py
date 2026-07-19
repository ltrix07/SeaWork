import pytest
from pydantic import ValidationError

from seawork.domain.enums import Provenance
from seawork.domain.inferred import Inferred


def test_source_provenance_requires_full_confidence() -> None:
    with pytest.raises(ValidationError):
        Inferred(value="GB", provenance=Provenance.SOURCE, confidence=0.9)


def test_rule_confidence_is_bounded() -> None:
    with pytest.raises(ValidationError):
        Inferred(value="GB", provenance=Provenance.RULE, confidence=1.1)
