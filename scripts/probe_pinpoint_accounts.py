"""Probe which employers publish a public Pinpoint postings feed (roadmap B3).

Pinpoint serves every account at {account}.pinpointhq.com/postings.json without
authentication. One adapter already handles that shape, so an account that
answers costs a line in sources/registry.yaml and no code at all — which makes
finding them the cheapest possible way to add sources.

This only asks whether a public endpoint exists. It sends an honest User-Agent,
reads robots.txt before the endpoint, waits between requests, and gives up on a
host after the first failure. Nothing here is authenticated, guessed past a
login, or retried against a refusal.

Usage:  .venv/bin/python scripts/probe_pinpoint_accounts.py
"""

import asyncio
import json
import sys
from pathlib import Path
from urllib.robotparser import RobotFileParser

import httpx

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seawork.config import Settings  # noqa: E402

DELAY_SECONDS = 1.0
TIMEOUT_SECONDS = 15.0

# Slugs are guesses at account names for employers already known to hire at sea.
# Cruise groups first, then ship management companies: both hire the roles our
# users look for, and both are the kind of employer that runs a hosted ATS.
CANDIDATES = [
    "hollandamericagroup",  # known good — proves the probe works
    # Carnival Corporation
    "carnival",
    "carnivalcruiseline",
    "princess",
    "princesscruises",
    "cunard",
    "pocruises",
    "aida",
    "aidacruises",
    "costa",
    "costacruises",
    "seabourn",
    "hollandamerica",
    # Royal Caribbean Group
    "royalcaribbean",
    "royalcaribbeangroup",
    "celebritycruises",
    "silversea",
    # Norwegian Cruise Line Holdings
    "norwegian",
    "ncl",
    "norwegiancruiseline",
    "oceania",
    "oceaniacruises",
    "regent",
    "regentsevenseas",
    # Independent cruise and expedition
    "msccruises",
    "disneycruiseline",
    "virginvoyages",
    "viking",
    "vikingcruises",
    "hurtigruten",
    "windstar",
    "windstarcruises",
    "explora",
    "exploraJourneys",
    "ponant",
    "azamara",
    "marella",
    "fredolsen",
    "swanhellenic",
    "atlasocean",
    "scenic",
    "emeraldcruises",
    "seadream",
    "lindblad",
    "auroraexpeditions",
    "quarkexpeditions",
    "saga",
    "ambassadorcruiseline",
    # Ship management and crewing
    "vships",
    "angloeastern",
    "bernhardschulte",
    "columbiashipmanagement",
    "wallem",
    "fleetmanagement",
    "synergymarine",
    "oldendorff",
    "maersk",
    "stenaline",
    "dfds",
    "wallenius",
    "hapaglloyd",
    "thome",
    "zodiacmaritime",
]


async def probe(client: httpx.AsyncClient, account: str, user_agent: str) -> dict[str, object]:
    base = f"https://{account}.pinpointhq.com"
    result: dict[str, object] = {"account": account}

    try:
        robots = await client.get(f"{base}/robots.txt")
    except httpx.HTTPError as error:
        result["status"] = "нет хоста"
        result["detail"] = type(error).__name__
        return result

    if robots.status_code == 200:
        parser = RobotFileParser()
        parser.parse(robots.text.splitlines())
        if not parser.can_fetch(user_agent, f"{base}/postings.json"):
            result["status"] = "robots запрещает"
            return result

    await asyncio.sleep(DELAY_SECONDS)

    try:
        response = await client.get(f"{base}/postings.json")
    except httpx.HTTPError as error:
        result["status"] = "ошибка"
        result["detail"] = type(error).__name__
        return result

    result["http"] = response.status_code
    if response.status_code != 200:
        result["status"] = f"HTTP {response.status_code}"
        return result

    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError:
        result["status"] = "не JSON"
        return result

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        result["status"] = "нет поля data"
        return result

    result["status"] = "есть"
    result["postings"] = len(data)
    titles = [str(row.get("title", "")) for row in data[:400] if isinstance(row, dict)]
    result["sample"] = titles[0][:60] if titles else ""
    return result


async def main() -> None:
    user_agent = Settings().user_agent
    print(f"User-Agent: {user_agent}")
    print(f"кандидатов: {len(CANDIDATES)}, пауза {DELAY_SECONDS}s\n")

    found: list[dict[str, object]] = []
    async with httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS, headers={"User-Agent": user_agent}, follow_redirects=True
    ) as client:
        for account in CANDIDATES:
            result = await probe(client, account, user_agent)
            if result["status"] == "есть":
                found.append(result)
                print(f"  ✓ {account:26} {result['postings']:5} вакансий   {result['sample']}")
            await asyncio.sleep(DELAY_SECONDS)

    print(f"\nнайдено аккаунтов: {len(found)}")
    total = sum(int(row["postings"]) for row in found)  # pyright: ignore[reportArgumentType]
    print(f"суммарно вакансий: {total}")


if __name__ == "__main__":
    asyncio.run(main())
