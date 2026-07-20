"""Probe maritime employers across ATS platforms with public job APIs (roadmap B9).

B3 established the pattern: a hosted ATS serves every customer at a predictable
URL, so one adapter opens every employer on that platform. Finding an account
therefore costs a line in the registry rather than a new parser.

This widens that sweep from Pinpoint to the platforms that publish a documented,
unauthenticated job endpoint. Nothing here guesses past a login: every URL below
is the platform's own public job-board API, the one their customers embed in
their careers page.

Politeness: honest User-Agent with contact, one request at a time, a pause
between them, and no retry after a failure.

Usage:  .venv/bin/python scripts/probe_ats_platforms.py
"""

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path

import httpx

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seawork.config import Settings  # noqa: E402

# Per host, not global. The first version paused 0.4s between requests but walked
# one platform at a time, so a single host took 60+ requests in a row and answered
# 429. Interleaving platforms spreads the load: each host now sees one request per
# full pass over the platform list.
DELAY_SECONDS = 0.4
TIMEOUT_SECONDS = 20.0
OUT = ROOT / "docs" / "ats-probe-results.json"


def _count_list(payload: object, *keys: str) -> int | None:
    """Return the length of the first list found under any of keys."""
    if isinstance(payload, list):
        return len(payload)
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return None


# url template -> how to count postings in the response
PLATFORMS: dict[str, tuple[str, Callable[[object], int | None]]] = {
    "recruitee": ("https://{slug}.recruitee.com/api/offers/", lambda p: _count_list(p, "offers")),
    "greenhouse": (
        "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        lambda p: _count_list(p, "jobs"),
    ),
    "lever": ("https://api.lever.co/v0/postings/{slug}?mode=json", _count_list),
    "ashby": (
        "https://api.ashbyhq.com/posting-api/job-board/{slug}",
        lambda p: _count_list(p, "jobs"),
    ),
    "smartrecruiters": (
        "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
        lambda p: _count_list(p, "content"),
    ),
    "workable": (
        "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
        lambda p: _count_list(p, "jobs"),
    ),
    "teamtailor": (
        "https://{slug}.teamtailor.com/jobs.json",
        lambda p: _count_list(p, "jobs", "data"),
    ),
}

# Employers already known to hire at sea, plus the crewing and ship-management
# companies that staff those vessels. Slugs are guesses at the account name.
EMPLOYERS = [
    # cruise and expedition
    "carnival",
    "princesscruises",
    "royalcaribbean",
    "celebritycruises",
    "silversea",
    "norwegian",
    "ncl",
    "oceaniacruises",
    "regentsevenseas",
    "msccruises",
    "virginvoyages",
    "viking",
    "vikingcruises",
    "hurtigruten",
    "windstar",
    "explora",
    "ponant",
    "lindblad",
    "quarkexpeditions",
    "aurora-expeditions",
    "seadream",
    "swanhellenic",
    "scenic",
    "emeraldcruises",
    # ship management and crewing
    "vships",
    "v-ships",
    "angloeastern",
    "anglo-eastern",
    "bernhardschulte",
    "columbia-shipmanagement",
    "wallem",
    "fleetmanagement",
    "synergymarine",
    "oldendorff",
    "thome",
    "zodiacmaritime",
    "marlow-navigation",
    "danica",
    # shipping lines and offshore
    "maersk",
    "hapag-lloyd",
    "stenaline",
    "dfds",
    "wallenius",
    "boskalis",
    "vanoord",
    "deme",
    "jandenul",
    "subsea7",
    "saipem",
    "tidewater",
    "bourbon",
    "solstad",
    "dof",
    "seacor",
    # diving, yachting, marine services
    "padi",
    "ssi",
    "divemaster",
    "yotspot",
    "burgessyachts",
    "fraseryachts",
    "northropandjohnson",
    "camperandnicholsons",
    # marine tech, already seen on Workable
    "marinetraffic",
    "deepbv",
]


async def probe(client: httpx.AsyncClient, platform: str, slug: str) -> tuple[str, str, int] | None:
    template, counter = PLATFORMS[platform]
    try:
        response = await client.get(template.format(slug=slug))
    except httpx.HTTPError:
        return None
    if response.status_code == 429:
        # Being throttled means our answers stop being trustworthy, not that the
        # account is missing. Say so loudly rather than recording a silent "no".
        print(f"  ! {platform}/{slug}: 429, результаты дальше недостоверны")
        return None
    if response.status_code != 200:
        return None
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError:
        return None
    count = counter(payload)
    if count is None:
        return None
    return platform, slug, count


async def main() -> None:
    user_agent = Settings().user_agent
    total = len(PLATFORMS) * len(EMPLOYERS)
    print(f"User-Agent: {user_agent}")
    print(f"платформ {len(PLATFORMS)}, работодателей {len(EMPLOYERS)}, запросов {total}")
    print(f"пауза {DELAY_SECONDS}s — примерно {total * DELAY_SECONDS / 60:.0f} мин\n")

    found: list[tuple[str, str, int]] = []
    async with httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS, headers={"User-Agent": user_agent}, follow_redirects=True
    ) as client:
        for slug in EMPLOYERS:
            for platform in PLATFORMS:
                hit = await probe(client, platform, slug)
                if hit is not None:
                    found.append(hit)
                    mark = "✓" if hit[2] else "·"
                    print(f"  {mark} {platform:16} {slug:24} {hit[2]:5} вакансий")
                await asyncio.sleep(DELAY_SECONDS)

    live = [row for row in found if row[2]]
    print(f"\nотвечают: {len(found)}, из них с вакансиями: {len(live)}")
    print(f"суммарно вакансий: {sum(row[2] for row in live)}")

    # The result is the deliverable, not the console output: it goes into
    # docs/sources-research.md and decides which contract gets written next.
    OUT.write_text(
        json.dumps(
            [{"platform": p, "slug": s, "postings": c} for p, s, c in found],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"записано: {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
