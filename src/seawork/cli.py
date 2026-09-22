import asyncio
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, cast

import typer
import yaml
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from seawork.config import Settings
from seawork.ingestion.enrich.cache import SqlLLMCache
from seawork.ingestion.enrich.llm import PROMPT_VERSION, LLMEnricher
from seawork.ingestion.enrich.providers import configured_client
from seawork.ingestion.enrich.rules import RulesEnricher
from seawork.ingestion.pipeline import IngestionPipeline, PipelineResult
from seawork.ingestion.references import load_yaml
from seawork.ingestion.sources.registry import RegisteredSource, build_source
from seawork.reporting.coverage import build_coverage_reports, format_coverage
from seawork.reporting.enrichment_eval import (
    FieldMetrics,
    aggregate_runs,
    diagnose_enrichment,
    evaluate_enrichment,
    format_aggregate_report,
    format_disagreements,
    format_enrichment_report,
    format_log_entry,
)
from seawork.storage.repository import Repository

app = typer.Typer(no_args_is_help=True)
ingest_app = typer.Typer(no_args_is_help=True)
report_app = typer.Typer(no_args_is_help=True)
eval_app = typer.Typer(no_args_is_help=True)
db_app = typer.Typer(no_args_is_help=True)
app.add_typer(ingest_app, name="ingest")
app.add_typer(report_app, name="report")
app.add_typer(eval_app, name="eval")
app.add_typer(db_app, name="db")


class IngestAction(StrEnum):
    RUN = "run"
    REFETCH = "refetch"
    REPROCESS = "reprocess"


def _pipeline(
    settings: Settings, registered: RegisteredSource, session: Session
) -> IngestionPipeline:
    rules = RulesEnricher(
        settings.reference_dir,
        direction=registered.config.direction,
        workplace=registered.config.workplace_type_hint,
    )
    enricher = LLMEnricher(
        rules,
        configured_client(settings),
        settings.reference_dir,
        cache=SqlLLMCache(session),
    )
    return IngestionPipeline(
        Repository(session),
        registered.source,
        enricher,
        settings.reference_dir / "opportunity_types.yaml",
    )


async def _execute(
    source_id: str, action: IngestAction, limit: int | None = None
) -> PipelineResult:
    settings = Settings()
    registered = build_source(source_id, settings)
    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as session:
            pipeline = _pipeline(settings, registered, session)
            if action is IngestAction.RUN:
                return await pipeline.run(limit)
            if action is IngestAction.REFETCH:
                return await pipeline.refetch(limit)
            return pipeline.reprocess()
    finally:
        await registered.source.close()
        engine.dispose()


def _show_result(result: PipelineResult) -> None:
    typer.echo(f"Получено raw: {result.fetched}; обработано: {result.processed}")


@ingest_app.command("run")
def ingest_run(
    source: Annotated[str, typer.Option("--source")],
    limit: Annotated[int | None, typer.Option("--limit", min=1)] = None,
) -> None:
    _show_result(asyncio.run(_execute(source, IngestAction.RUN, limit)))


@ingest_app.command("refetch")
def ingest_refetch(source: Annotated[str, typer.Option("--source")]) -> None:
    _show_result(asyncio.run(_execute(source, IngestAction.REFETCH)))


@ingest_app.command("reprocess")
def ingest_reprocess(source: Annotated[str, typer.Option("--source")]) -> None:
    _show_result(asyncio.run(_execute(source, IngestAction.REPROCESS)))


@report_app.command("coverage")
def report_coverage(source: Annotated[str, typer.Option("--source")]) -> None:
    settings = Settings()
    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as session:
            reports = build_coverage_reports(session, source)
            if not reports:
                raise typer.BadParameter(f"Нет данных для источника {source}")
            typer.echo("\n\n".join(format_coverage(report) for report in reports))
    finally:
        engine.dispose()


@eval_app.command("enrichment")
def eval_enrichment(
    provider: Annotated[str, typer.Option("--provider")],
    show_errors: Annotated[bool, typer.Option("--show-errors")] = False,
    runs: Annotated[int, typer.Option("--runs", min=1)] = 1,
    log: Annotated[bool, typer.Option("--log/--no-log")] = True,
) -> None:
    """Run one configured provider against the immutable hand-labelled gold set."""
    settings = Settings()
    client = configured_client(settings)
    if client is None or settings.llm_provider != provider:
        raise typer.BadParameter("Провайдер не настроен или отключён", param_hint="--provider")
    data = load_yaml(settings.reference_dir / "certificates.yaml")
    rows = cast(list[object], data.get("certificates", []))
    certificate_keys: list[str] = []
    certificate_patterns: dict[str, list[str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        entry = cast(dict[str, object], row)
        key = entry.get("key")
        if not isinstance(key, str):
            continue
        certificate_keys.append(key)
        patterns = entry.get("patterns", [])
        certificate_patterns[key] = (
            [p for p in cast(list[object], patterns) if isinstance(p, str)]
            if isinstance(patterns, list)
            else []
        )
    root = Path(__file__).resolve().parents[2]
    gold_path = root / "tests/fixtures/eval/enrichment_gold.yaml"
    reports: list[dict[str, FieldMetrics]] = []
    for index in range(runs):
        if show_errors:
            report, errors = diagnose_enrichment(
                client, gold_path, certificate_keys, certificate_patterns
            )
        else:
            report = evaluate_enrichment(client, gold_path, certificate_keys, certificate_patterns)
            errors = []
        reports.append(report)
        if runs > 1:
            typer.echo(f"--- прогон {index + 1} из {runs} ---")
        typer.echo(format_enrichment_report(provider, report))
        if show_errors:
            typer.echo(format_disagreements(errors))

    model_id = client.model_id
    if runs > 1:
        spreads = aggregate_runs(reports)
        typer.echo("")
        typer.echo(format_aggregate_report(provider, model_id, runs, spreads))
    else:
        spreads = aggregate_runs(reports)

    if not log:
        return
    # Appended, never rewritten: a measurement is only worth keeping if a later one
    # can be compared against it unchanged.
    gold_size = len(cast(list[object], yaml.safe_load(gold_path.read_text())["records"]))
    entry = format_log_entry(
        provider,
        model_id,
        PROMPT_VERSION,
        runs,
        gold_size,
        spreads,
        datetime.now(UTC).strftime("%d.%m.%Y"),
    )
    log_path = root / "docs/enrichment-eval-log.md"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("\n" + entry)
    typer.echo(f"Записано в {log_path.relative_to(root)}")


@db_app.command("upgrade")
def db_upgrade() -> None:
    project_root = Path(__file__).resolve().parents[2]
    config = Config(project_root / "alembic.ini")
    command.upgrade(config, "head")


if __name__ == "__main__":
    app()
