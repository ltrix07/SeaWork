"""What to show about one recommendation, independent of the channel that shows it."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, HttpUrl

from seawork.domain.match import FactorOutcome, FactorVerdict, MatchScore
from seawork.domain.models import SalaryRange
from seawork.domain.recommendation import Candidate, Recommendation


class Reason(BaseModel):
    model_config = ConfigDict(frozen=True)

    factor: str
    outcome: FactorOutcome
    evidence: str | None


class Card(BaseModel):
    model_config = ConfigDict(frozen=True)

    opportunity_id: int
    title: str
    employer: str | None
    url: HttpUrl
    direction: str | None
    salary: str | None
    reasons: tuple[Reason, ...]
    caveats: tuple[str, ...]
    similar_count: int


class PreparedCard(BaseModel):
    model_config = ConfigDict(frozen=True)

    card: Card
    recommendation: Recommendation


def card_from(candidate: Candidate, score: MatchScore, similar_count: int) -> Card:
    opportunity = candidate.opportunity
    direction = opportunity.direction
    salary = opportunity.salary
    return Card(
        opportunity_id=candidate.opportunity_id,
        title=opportunity.normalized.title,
        employer=candidate.employer,
        url=opportunity.normalized.url,
        direction=None if direction is None else direction.value,
        salary=None if salary is None else _format_salary(salary.value),
        reasons=_reasons(score.verdicts),
        caveats=tuple(dict.fromkeys(c for verdict in score.verdicts for c in verdict.caveats)),
        similar_count=similar_count,
    )


def _reasons(verdicts: list[FactorVerdict]) -> tuple[Reason, ...]:
    by_weight = sorted(verdicts, key=lambda verdict: -verdict.weight)
    matches = [v for v in by_weight if v.outcome is FactorOutcome.MATCH]
    mismatches = [v for v in by_weight if v.outcome is FactorOutcome.MISMATCH]
    shown = matches + mismatches[:1]
    return tuple(
        Reason(factor=v.factor, outcome=v.outcome, evidence=v.opportunity_evidence) for v in shown
    )


def _format_salary(salary: SalaryRange) -> str | None:
    period = "" if salary.period is None else f" / {salary.period}"
    if salary.minimum is not None and salary.maximum is not None:
        amount = f"{_amount(salary.minimum)}-{_amount(salary.maximum)}"
    elif salary.minimum is not None:
        amount = f">= {_amount(salary.minimum)}"
    elif salary.maximum is not None:
        amount = f"<= {_amount(salary.maximum)}"
    else:
        return None
    return f"{amount} {salary.currency}{period}"


def _amount(value: Decimal) -> str:
    return format(value.normalize(), "f")
