"""Build the hand-labelling template for the enrichment gold set.

Selects a stratified sample from the records where RulesEnricher returned None,
which is exactly the population the LLM enricher will run on
(docs/modules/llm-enricher.md section 7).

Stratification is by mechanical signals only — whether the text mentions
experience, whether it mentions anything certificate-shaped. Those signals
correlate with difficulty but carry no judgement; the labels themselves are the
human's job. Records with no signal at all are deliberately over-sampled: they
are the false-fill test, and false fills are the metric that matters most.

The sample is deterministic (fixed seed) so it can be regenerated and diffed.

Usage:  .venv/bin/python scripts/build_eval_sample.py
"""

import datetime
import hashlib
import json
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from seawork.domain.enums import SourceTrust  # noqa: E402
from seawork.ingestion.base import RawItem  # noqa: E402
from seawork.ingestion.classify import classify  # noqa: E402
from seawork.ingestion.enrich.rules import RulesEnricher  # noqa: E402
from seawork.ingestion.quality import check_quality  # noqa: E402
from seawork.ingestion.sources.sites.crewplanet import CrewplanetSource  # noqa: E402
from tests.test_pinpoint import make_raw, make_source  # noqa: E402

REFERENCE_DIR = ROOT / "data" / "reference"
RULES = REFERENCE_DIR / "opportunity_types.yaml"
OUT = ROOT / "tests" / "fixtures" / "eval" / "enrichment_gold.yaml"

TOTAL = 50
SEED = 20260720

# Part of the Crewplanet corpus is written in Russian. English-only hints put those
# records in the no-signal stratum, which is meant to be the false-fill test — a
# record that states its requirement in Russian is the opposite of no signal, and
# its presence there quietly corrupts the metric.
EXPERIENCE_HINT = re.compile(
    r"experience|years?|months?|rank|опыт|стаж|контракт|должност", re.IGNORECASE
)
CERT_HINT = re.compile(
    r"certificat|licen[cs]e|STCW|COC|endorsement|training|diploma|qualif"
    r"|сертификат|диплом|удостоверен|документ",
    re.IGNORECASE,
)


def collect() -> list[tuple[str, str, str, str, str]]:
    """Return (source_id, external_id, url, title, text) for LLM-bound records."""
    rows: list[tuple[str, str, str, str, str]] = []

    crewplanet = CrewplanetSource(
        source_id="crewplanet", trust=SourceTrust.PRIMARY, user_agent="eval", interval_seconds=0
    )
    enricher = RulesEnricher(REFERENCE_DIR, direction="general_maritime", workplace="vessel")
    xml = (ROOT / "tests" / "fixtures" / "crewplanet" / "vacancies.xml").read_text(encoding="utf-8")
    for raw_xml in re.findall(r"<item>.*?</item>", xml, re.S):
        external_id = re.search(r"vacId/(\d+)", raw_xml).group(1)  # pyright: ignore[reportOptionalMemberAccess]
        link = re.search(r"<link>(.*?)</link>", raw_xml, re.S).group(1).strip()  # pyright: ignore[reportOptionalMemberAccess]
        item = RawItem.model_validate(
            {
                "source_id": "crewplanet",
                "external_id": external_id,
                "url": link,
                "fetched_at": datetime.datetime.now(datetime.UTC),
                "payload": raw_xml,
                "content_type": "application/xml",
                "content_hash": hashlib.sha256(raw_xml.encode()).hexdigest(),
                "http_status": 200,
            }
        )
        normalized = crewplanet.normalize(item)
        enriched = enricher.enrich(
            normalized,
            classify(normalized, RULES),
            check_quality(normalized, duplicate_content=False),
        )
        if enriched.experience_level is None or enriched.required_certificates is None:
            text = RulesEnricher.qualification_text(normalized)
            rows.append(("crewplanet", external_id, str(normalized.url), normalized.title, text))

    pinpoint = make_source()
    cruise = RulesEnricher(REFERENCE_DIR, direction="cruise", workplace="vessel")
    postings = json.loads(
        (ROOT / "tests" / "fixtures" / "pinpoint" / "hollandamericagroup.json").read_text()
    )["data"]
    for posting in postings:
        normalized = pinpoint.normalize(make_raw(posting))
        enriched = cruise.enrich(
            normalized,
            classify(normalized, RULES),
            check_quality(normalized, duplicate_content=False),
        )
        if enriched.experience_level is None or enriched.required_certificates is None:
            text = RulesEnricher.qualification_text(normalized)
            rows.append(
                (
                    "pinpoint:hollandamericagroup",
                    normalized.external_id,
                    str(normalized.url),
                    normalized.title,
                    text,
                )
            )
    return rows


MIN_TEXT = 20


def stratum(source_id: str, text: str) -> str:
    short = "pinpoint" if source_id.startswith("pinpoint") else "crewplanet"
    has_exp = bool(EXPERIENCE_HINT.search(text))
    has_cert = bool(CERT_HINT.search(text))
    if not has_exp and not has_cert:
        return f"{short}/no-signal"
    if has_exp and has_cert:
        return f"{short}/both"
    return f"{short}/exp-only" if has_exp else f"{short}/cert-only"


def yaml_block(text: str, indent: str = "    ") -> str:
    body = "\n".join(indent + line for line in (text or "(пусто)").splitlines()) or indent
    return body


def main() -> None:
    all_rows = collect()
    # Records with no usable text are excluded: there is nothing to label, and
    # nothing for a model to read either. The enricher must skip them outright
    # rather than spend a call proving an empty string contains nothing.
    rows = [row for row in all_rows if len(row[4].strip()) >= MIN_TEXT]
    empty = len(all_rows) - len(rows)

    buckets: dict[str, list[tuple[str, str, str, str, str]]] = defaultdict(list)
    for row in rows:
        buckets[stratum(row[0], row[4])].append(row)

    rng = random.Random(SEED)
    for bucket in buckets.values():
        rng.shuffle(bucket)

    # 25/25 between sources; inside each source the no-signal stratum is
    # over-sampled because those records are the false-fill test.
    quotas: dict[str, int] = {}
    for short in ("crewplanet", "pinpoint"):
        present = {k: v for k, v in buckets.items() if k.startswith(short) and v}
        no_signal = f"{short}/no-signal"
        budget = TOTAL // 2
        if no_signal in present:
            quotas[no_signal] = min(len(present[no_signal]), 8)
            budget -= quotas[no_signal]
        rest = [k for k in present if k != no_signal]
        for index, key in enumerate(rest):
            share = budget // len(rest) + (1 if index < budget % len(rest) else 0)
            quotas[key] = min(len(present[key]), share)

    # Some strata are smaller than their quota (there are only so many
    # certificate-only Crewplanet records). Redistribute the shortfall to the
    # strata that still have records left, so the sample always lands on TOTAL.
    shortfall = TOTAL - sum(quotas.values())
    while shortfall > 0:
        candidates = [k for k, v in buckets.items() if v and quotas.get(k, 0) < len(v)]
        if not candidates:
            break
        candidates.sort(key=lambda k: len(buckets[k]) - quotas.get(k, 0), reverse=True)
        quotas[candidates[0]] = quotas.get(candidates[0], 0) + 1
        shortfall -= 1

    sample: list[tuple[str, tuple[str, str, str, str, str]]] = []
    for key, count in sorted(quotas.items()):
        for row in buckets[key][:count]:
            sample.append((key, row))
    rng.shuffle(sample)

    lines = [
        "# Эталон для оценки LLM-обогатителя (docs/modules/llm-enricher.md §7).",
        "#",
        "# ЗАПОЛНЯЕТСЯ ВРУЧНУЮ человеком, знающим отрасль. Размечать моделью нельзя:",
        "# тогда мы измерим согласие модели с самой собой, а не её точность.",
        "#",
        "# По каждой записи заполнить пять полей:",
        "#",
        "#   experience_level      entry | junior | mid | senior | lead | null",
        "#                         null — если требования к опыту в тексте НЕТ.",
        "#                         Это частый и совершенно нормальный ответ:",
        "#                         именно на нём измеряется доля ложных заполнений.",
        "#",
        "#                         Срок назван числом — решает число:",
        "#                           опыт не требуется, trainee   entry",
        "#                           менее 2 лет                  junior",
        "#                           от 2 до 4 лет                mid",
        "#                           5 лет и более                senior",
        "#                         Срок не назван, но требование есть           junior",
        "#                         («previous experience in rank») — берём нижнюю границу.",
        "#",
        "#                         lead — ТОЛЬКО когда требуется прошлый опыт руководства",
        "#                         И числа нет («Proven experience leading large teams»).",
        "#                         При «Minimum 3-years leadership experience» ставится mid:",
        "#                         число сильнее. Иначе эталон разойдётся с правилами, которые",
        "#                         умеют выдавать только числовые уровни, и мы будем штрафовать",
        "#                         модель за согласие с нашим же кодом.",
        "#",
        "# ОБЩИЙ ПРИНЦИП ОБОИХ ПОЛЕЙ: размечается то, что кандидат обязан принести с собой,",
        "# а не то, в чём состоит работа. Обязанность и компетенция — не требование к опыту:",
        "#   «Ability to train and guide assigned waiting staff»  — навык в списке, НЕ lead",
        "#   «Manage the training of Chief Stewards»              — обязанность, НЕ lead",
        "#   «Proven experience leading large teams»              — требуемый опыт, lead",
        "# Тот же принцип уже действует для сертификатов: «in accordance with USPH standards»",
        "# — это стандарт работы, а не документ на руках, и ключ usph не ставится.",
        "#",
        "#   experience_basis      stated   — требование сформулировано прямо",
        "#                                    («Minimum 2 years experience»)",
        "#                         inferred — выведено из формулировки",
        "#                                    («experience in rank on similar vessel type»)",
        "#                         null     — если experience_level = null",
        "#",
        "#   required_certificates список ключей из data/reference/certificates.yaml,",
        "#                         [] если сертификаты не требуются.",
        "#",
        "#   experience_quote      точная подстрока из поля text, обосновывающая уровень;",
        "#   certificate_quotes    null / {} если значения нет. Цитата копируется символ",
        "#                         в символ: она проверяется поиском по text, и любая",
        "#                         правка пунктуации или регистра означает провал проверки.",
        "#                         Если процитировать нечем — значит, обоснования в тексте",
        "#                         нет, и верный ответ null.",
        "#",
        "# Размечается ТОЛЬКО поле text — тот же текст, что получит модель. Не открывать",
        "# страницу вакансии по url: на ней есть данные, которых у модели не будет, и",
        "# эталон начнёт требовать невозможного.",
        "#",
        "# Не засчитывать за требование к опыту: срок контракта («Contract Duration: 5 months»),",
        "# периодичность переаттестации («Food Hygiene course every 2 years»), возраст судна.",
        "#",
        "# Поле notes — для сомнительных случаев: почему решение неочевидно.",
        "#",
        f"# Выборка детерминирована (seed={SEED}), пересобирается:",
        "#   .venv/bin/python scripts/build_eval_sample.py",
        "",
        "records:",
    ]

    for key, (source_id, external_id, url, title, text) in sample:
        lines += [
            f"  - source_id: {source_id}",
            f'    external_id: "{external_id}"',
            f"    stratum: {key}",
            f"    url: {url}",
            f"    title: {json.dumps(title, ensure_ascii=False)}",
            "    text: |",
            yaml_block(text, "      "),
            "    # --- заполнить вручную ---",
            "    experience_level: null",
            "    experience_basis: null",
            "    experience_quote: null",
            "    required_certificates: []",
            "    certificate_quotes: {}",
            '    notes: ""',
            "",
        ]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")

    print(f"кандидатов (правила вернули None): {len(all_rows)}")
    print(f"  из них без пригодного текста:    {empty}  — исключены, LLM их не увидит")
    print(f"  размечаемых:                     {len(rows)}")
    print(f"в выборке: {len(sample)}\n")
    print("распределение по стратам:")
    counts: dict[str, int] = defaultdict(int)
    for key, _ in sample:
        counts[key] += 1
    for key in sorted(counts):
        print(f"  {key:28} {counts[key]:2}   (доступно {len(buckets[key])})")
    print(f"\nзаписано: {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
