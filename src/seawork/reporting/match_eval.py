"""Agreement of Match Score with the owner's labels on the gold set.

The pairs are scored again on the frozen dump at the moment of the report, so the
report measures the current formula, not the one that was in force when the pairs
were sampled. There is no threshold here: an absolute percent cannot be interpreted
before the weights are calibrated, only the ordering and the costly mistakes can.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import product
from typing import Literal

from seawork.domain.match import FactorOutcome, FactorVerdict, MatchScore
from seawork.recommendation.engine import rank_tier
from seawork.reporting.match_gold import (
    GoldPair,
    Label,
    ProfileKind,
    ProfileSpec,
    profile_of_pair,
)
from seawork.scoring.engine import compute_match_score

KIND_TITLES: dict[ProfileKind, str] = {
    "onboarding": "форма онбординга (что бот знает о человеке)",
    "extended": "расширенный профиль (будущая аудитория)",
}


@dataclass(frozen=True)
class ScoredPair:
    pair: GoldPair
    label: Label
    score: MatchScore

    @property
    def value(self) -> float | None:
        return self.score.value

    @property
    def verdicts(self) -> list[FactorVerdict]:
        return self.score.verdicts

    @property
    def in_feed(self) -> bool:
        return self.score.value != 0.0


@dataclass(frozen=True)
class OrderStats:
    """Fit-above-no-fit comparisons inside one profile, as the feed orders them.

    Only pairs that reach the feed (`value != 0.0`) are compared; `0.0` is counted by
    its own metric. The key is the feed's own (`rank_tier`): a positive value before
    `None`, then the higher value, then more comparable factors. Freshness and id are
    not part of it. A tie is a full equality of that key: the feed cannot tell the two
    apart, and it counts as half a correct comparison.
    """

    compared: int = 0
    correct: int = 0
    ties: int = 0
    wrong: int = 0

    @property
    def accuracy(self) -> float | None:
        return (self.correct + 0.5 * self.ties) / self.compared if self.compared else None

    def __add__(self, other: "OrderStats") -> "OrderStats":
        return OrderStats(
            self.compared + other.compared,
            self.correct + other.correct,
            self.ties + other.ties,
            self.wrong + other.wrong,
        )


@dataclass(frozen=True)
class GroupMetrics:
    fit: int
    no_fit: int
    unsure: int
    order: OrderStats
    fit_zero: int
    no_fit_zero: int
    fit_none: int
    fit_with_facts: int
    unsure_none: int
    unsure_zero: int

    @property
    def fit_zero_rate(self) -> float | None:
        return self.fit_zero / self.fit if self.fit else None

    @property
    def no_fit_zero_rate(self) -> float | None:
        return self.no_fit_zero / self.no_fit if self.no_fit else None

    @property
    def fit_none_rate(self) -> float | None:
        """Over the fit pairs of profiles that state something; `None` when there are none."""
        return self.fit_none / self.fit_with_facts if self.fit_with_facts else None


@dataclass(frozen=True)
class Disagreement:
    """`fit_none`/`fit_zero`: a fit pair the feed would lose or drop.

    `inversion`: a fit pair with the no-fit pairs (`others`) that the feed puts above it.
    """

    kind: Literal["fit_none", "fit_zero", "inversion"]
    profile_id: str
    fit: ScoredPair
    others: tuple[ScoredPair, ...] = ()


@dataclass(frozen=True)
class AgreementReport:
    total: int
    unlabelled: int
    groups: dict[ProfileKind, GroupMetrics]
    profiles: dict[str, GroupMetrics]
    profile_kinds: dict[str, ProfileKind]
    structural_none: dict[str, bool]
    disagreements: list[Disagreement] = field(default_factory=list[Disagreement])


def score_labelled(profiles: Sequence[ProfileSpec], pairs: Sequence[GoldPair]) -> list[ScoredPair]:
    specs = {spec.id: spec for spec in profiles}
    scored: list[ScoredPair] = []
    for pair in pairs:
        spec = profile_of_pair(specs, pair)
        if pair.label is None:
            continue
        scored.append(
            ScoredPair(pair, pair.label, compute_match_score(spec.to_profile(), pair.opportunity))
        )
    return scored


def _order(items: Sequence[ScoredPair]) -> tuple[OrderStats, list[Disagreement]]:
    fits = [item for item in items if item.label is Label.FIT and item.in_feed]
    no_fits = [item for item in items if item.label is Label.NO_FIT and item.in_feed]
    ties = correct = 0
    beaten_by: dict[int, list[ScoredPair]] = {}
    for fit, no_fit in product(fits, no_fits):
        fit_key, no_fit_key = rank_tier(fit.score), rank_tier(no_fit.score)
        if fit_key < no_fit_key:
            correct += 1
        elif fit_key == no_fit_key:
            ties += 1
        else:
            beaten_by.setdefault(id(fit), []).append(no_fit)
    wrong = sum(len(others) for others in beaten_by.values())
    inversions = [
        Disagreement("inversion", fit.pair.profile_id, fit, tuple(beaten_by[id(fit)]))
        for fit in fits
        if id(fit) in beaten_by
    ]
    return OrderStats(len(fits) * len(no_fits), correct, ties, wrong), inversions


def _metrics(per_profile: Sequence[tuple[ProfileSpec, Sequence[ScoredPair]]]) -> GroupMetrics:
    items = [item for _, group in per_profile for item in group]
    fits = [item for item in items if item.label is Label.FIT]
    no_fits = [item for item in items if item.label is Label.NO_FIT]
    unsure = [item for item in items if item.label is Label.UNSURE]
    with_facts = [item for spec, group in per_profile if not spec.structural_none for item in group]
    order = OrderStats()
    for _, group in per_profile:
        order += _order(group)[0]
    fits_with_facts = [item for item in with_facts if item.label is Label.FIT]
    return GroupMetrics(
        fit=len(fits),
        no_fit=len(no_fits),
        unsure=len(unsure),
        order=order,
        fit_zero=sum(1 for item in fits if not item.in_feed),
        no_fit_zero=sum(1 for item in no_fits if not item.in_feed),
        fit_none=sum(1 for item in fits_with_facts if item.value is None),
        fit_with_facts=len(fits_with_facts),
        unsure_none=sum(1 for item in unsure if item.value is None),
        unsure_zero=sum(1 for item in unsure if not item.in_feed),
    )


def evaluate_agreement(
    profiles: Sequence[ProfileSpec], pairs: Sequence[GoldPair]
) -> AgreementReport:
    scored = score_labelled(profiles, pairs)
    by_profile = {
        spec.id: [item for item in scored if item.pair.profile_id == spec.id] for spec in profiles
    }
    disagreements: list[Disagreement] = []
    for spec in profiles:
        items = by_profile[spec.id]
        for item in items:
            if item.label is not Label.FIT:
                continue
            if not item.in_feed:
                disagreements.append(Disagreement("fit_zero", spec.id, item))
            elif item.value is None and not spec.structural_none:
                disagreements.append(Disagreement("fit_none", spec.id, item))
        disagreements.extend(_order(items)[1])
    groups: dict[ProfileKind, GroupMetrics] = {
        kind: _metrics([(spec, by_profile[spec.id]) for spec in profiles if spec.kind == kind])
        for kind in KIND_TITLES
    }
    return AgreementReport(
        total=len(pairs),
        unlabelled=sum(1 for pair in pairs if pair.label is None),
        groups=groups,
        profiles={spec.id: _metrics([(spec, by_profile[spec.id])]) for spec in profiles},
        profile_kinds={spec.id: spec.kind for spec in profiles},
        structural_none={spec.id: spec.structural_none for spec in profiles},
        disagreements=disagreements,
    )


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _ratio(count: int, rate: float | None) -> str:
    return f"{count} ({_pct(rate)})"


def _order_text(order: OrderStats) -> str:
    return (
        f"{_pct(order.accuracy)} "
        f"(сравнений {order.compared}: верно {order.correct}, ничьих {order.ties}, "
        f"наоборот {order.wrong})"
    )


def format_agreement_report(report: AgreementReport) -> str:
    lines = [
        f"Пар в эталоне: {report.total}; без метки: {report.unlabelled} (в метрики не входят)",
        "Оценка пересчитана текущей формулой на замороженных вакансиях.",
    ]
    for kind, metrics in report.groups.items():
        lines.append("")
        lines.append(f"== {KIND_TITLES[kind]}")
        lines.append(
            f"метки: подходит {metrics.fit}, не подходит {metrics.no_fit}, спорно {metrics.unsure}"
        )
        lines.append(f"порядок «подходит» выше «не подходит»: {_order_text(metrics.order)}")
        lines.append(
            f"исключено из выдачи (0.0): «не подходит» верно "
            f"{_ratio(metrics.no_fit_zero, metrics.no_fit_zero_rate)}; «подходит» ошибочно "
            f"{_ratio(metrics.fit_zero, metrics.fit_zero_rate)}"
        )
        lines.append(
            f"«подходит» с value=None (профили с фактами): "
            f"{_ratio(metrics.fit_none, metrics.fit_none_rate)}"
        )
        lines.append(
            f"спорно (отдельно): value=None {metrics.unsure_none}, value=0.0 {metrics.unsure_zero}"
        )
        for profile_id, profile_metrics in report.profiles.items():
            if report.profile_kinds[profile_id] != kind:
                continue
            none_text = (
                "None структурный"
                if report.structural_none[profile_id]
                else f"None {profile_metrics.fit_none}"
            )
            lines.append(
                f"  {profile_id}: подходит {profile_metrics.fit}, не подходит "
                f"{profile_metrics.no_fit}, спорно {profile_metrics.unsure}; порядок "
                f"{_order_text(profile_metrics.order)}; {none_text}; "
                f"0.0 у «подходит» {profile_metrics.fit_zero}, у «не подходит» "
                f"{profile_metrics.no_fit_zero}"
            )
    return "\n".join(lines)


def _value_text(value: float | None) -> str:
    return "None" if value is None else f"{value:.3f}"


def _factors_text(verdicts: Sequence[FactorVerdict]) -> str:
    marks = {
        FactorOutcome.MATCH: "+",
        FactorOutcome.MISMATCH: "-",
        FactorOutcome.NOT_COMPARABLE: "?",
    }
    parts: list[str] = []
    for verdict in verdicts:
        text = f"{verdict.factor}{marks[verdict.outcome]}"
        if verdict.caveats:
            text += f"[{', '.join(verdict.caveats)}]"
        parts.append(text)
    return " ".join(parts)


def _pair_text(item: ScoredPair) -> str:
    note = f" · заметка: {item.pair.note}" if item.pair.note else ""
    return (
        f"{item.pair.source_id}/{item.pair.external_id} «{item.pair.title}» "
        f"value={_value_text(item.value)} · {_factors_text(item.verdicts)}{note}"
    )


_DISAGREEMENT_TITLES = {
    "fit_none": "подходит, но value=None",
    "fit_zero": "подходит, но value=0.0 (вакансия исключена из выдачи)",
    "inversion": "«подходит» обогнали «не подходит» (выдача ставит их выше)",
}


def format_disagreements(report: AgreementReport) -> str:
    """Factors: `+` match, `-` mismatch, `?` not comparable; caveats in brackets."""
    if report.unlabelled == report.total:
        return "Размеченных пар нет, расхождений считать не из чего."
    if not report.disagreements:
        return "Расхождений нет."
    lines = ["Расхождения (+ совпало, - не совпало, ? не сравнимо):"]
    for kind in _DISAGREEMENT_TITLES:
        group = [item for item in report.disagreements if item.kind == kind]
        if not group:
            continue
        lines.append("")
        lines.append(f"-- {_DISAGREEMENT_TITLES[kind]}: {len(group)}")
        for item in group:
            lines.append(f"[{item.profile_id}] подходит: {_pair_text(item.fit)}")
            lines.extend(f"    обогнала, не подходит: {_pair_text(other)}" for other in item.others)
    return "\n".join(lines)


def format_log_entry(report: AgreementReport, measured_at: str) -> str:
    """One journal entry. Appended, never rewritten: the point is comparability."""
    lines = [
        f"## {measured_at}",
        "",
        f"Пар в эталоне {report.total}, без метки {report.unlabelled}. "
        "Оценка пересчитана текущей формулой.",
        "",
        "| Группа профилей | Подходит / не подходит / спорно | Порядок "
        "| `0.0` у «не подходит» (верно) | `0.0` у «подходит» (ошибка) "
        "| `None` у «подходит» (профили с фактами) |",
        "|---|---|---|---|---|---|",
    ]
    for kind, metrics in report.groups.items():
        lines.append(
            f"| {KIND_TITLES[kind]} | {metrics.fit} / {metrics.no_fit} / {metrics.unsure} | "
            f"{_order_text(metrics.order)} | "
            f"{_ratio(metrics.no_fit_zero, metrics.no_fit_zero_rate)} | "
            f"{_ratio(metrics.fit_zero, metrics.fit_zero_rate)} | "
            f"{_ratio(metrics.fit_none, metrics.fit_none_rate)} |"
        )
    lines.append("")
    lines.extend(["```text", format_disagreements(report), "```", ""])
    return "\n".join(lines)
