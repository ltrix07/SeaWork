from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from seawork.domain.enums import OpportunityStatus, Provenance
from seawork.storage.models import OpportunityRecord


@dataclass(frozen=True)
class FieldCoverage:
    present: int
    total: int
    provenance: dict[str, int]


@dataclass(frozen=True)
class CoverageReport:
    source_id: str
    trust: str
    total: int
    rejected: int
    active: int
    fields: dict[str, FieldCoverage]
    rejection_reasons: dict[str, int]


NORMALIZED_FIELDS = ("title", "description", "url", "employer", "posted_at")
ENRICHED_FIELDS = (
    "country",
    "city",
    "profession",
    "direction",
    "required_certificates",
    "experience_level",
    "salary",
)


def build_coverage_reports(session: Session, source_id: str) -> list[CoverageReport]:
    records = list(
        session.scalars(select(OpportunityRecord).where(OpportunityRecord.source_id == source_id))
    )
    by_trust: dict[str, list[OpportunityRecord]] = defaultdict(list)
    for record in records:
        by_trust[record.source_trust].append(record)
    return [_build_one(source_id, trust, rows) for trust, rows in sorted(by_trust.items())]


def _build_one(source_id: str, trust: str, records: list[OpportunityRecord]) -> CoverageReport:
    active = [row for row in records if row.status == OpportunityStatus.ACTIVE.value]
    rejection_reasons: Counter[str] = Counter()
    for row in records:
        if row.status == OpportunityStatus.REJECTED.value:
            rejection_reasons.update(row.quality_flags)

    fields: dict[str, FieldCoverage] = {}
    for name in NORMALIZED_FIELDS:
        present = sum(bool(getattr(row, name)) for row in active)
        fields[name] = FieldCoverage(
            present=present,
            total=len(active),
            provenance={Provenance.SOURCE.value: present},
        )
    fields["type"] = FieldCoverage(
        present=len(active), total=len(active), provenance={Provenance.SOURCE.value: len(active)}
    )
    for name in ENRICHED_FIELDS:
        present = 0
        provenance: Counter[str] = Counter()
        for row in active:
            inferred_value = row.enriched.get(name)
            inferred = (
                cast(dict[str, object], inferred_value)
                if isinstance(inferred_value, dict)
                else None
            )
            if inferred is None:
                continue
            value = inferred.get("value")
            if value is None or value == [] or value == "":
                continue
            present += 1
            origin = inferred.get("provenance")
            if isinstance(origin, str):
                provenance[origin] += 1
        fields[name] = FieldCoverage(present, len(active), dict(provenance))
    return CoverageReport(
        source_id=source_id,
        trust=trust,
        total=len(records),
        rejected=len(records) - len(active),
        active=len(active),
        fields=fields,
        rejection_reasons=dict(rejection_reasons),
    )


def format_coverage(report: CoverageReport) -> str:
    rejected_percentage = report.rejected / report.total * 100 if report.total else 0.0
    lines = [
        f"Источник: {report.source_id}",
        f"Доверие: {report.trust}",
        f"Обойдено объектов:            {report.total}",
        f"Отклонено на quality gate:     {report.rejected} ({rejected_percentage:.1f}%)",
        f"Активных:                     {report.active}",
        "",
        "Заполняемость полей:",
    ]
    for name, field in report.fields.items():
        percentage = field.present / field.total * 100 if field.total else 0.0
        origins = ", ".join(
            f"{origin} {count / field.total * 100:.1f}%" if field.total else origin
            for origin, count in sorted(field.provenance.items())
        )
        lines.append(
            f"  {name:<22} {field.present:>4} / {field.total:<4} {percentage:>6.1f}%"
            f"   [{origins or '—'}]"
        )
    lines.extend(["", "Причины отклонения:"])
    if report.rejection_reasons:
        lines.extend(
            f"  {reason:<24} {count}" for reason, count in sorted(report.rejection_reasons.items())
        )
    else:
        lines.append("  —")
    return "\n".join(lines)
