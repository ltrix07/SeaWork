"""Cards and profile as Telegram HTML. Pure: no bot, no I/O, no decisions."""

from collections.abc import Sequence
from html import escape

from seawork.app import Card, FeedPage, Names, Reason
from seawork.bot import texts
from seawork.domain.match import FactorOutcome
from seawork.domain.profile import UserProfile

MAX_MATCH_LINES = 3
MAX_TITLE = 200
MAX_EVIDENCE = 100
MAX_COUNTRIES = 3
MAX_SIMILAR_LISTED = 20
MAX_SIMILAR_TITLE = 100
MAX_MESSAGE = 4096
FOOTER_ALLOWANCE = 40


def render_feed(page: FeedPage, names: Names) -> str:
    if page.card is None:
        return texts.FEED_DONE if page.total else texts.EMPTY_FEED
    header = texts.FEED_HEADER.format(position=page.index + 1, total=page.total)
    return f"{header}\n\n{render_card(page.card, names)}"


def render_card(card: Card, names: Names) -> str:
    emoji = None if card.profession is None else texts.PROFESSION_EMOJI.get(card.profession)
    title = _clip(card.title, MAX_TITLE)
    lines = [f"<b>{title}</b>" if emoji is None else f"<b>{emoji} {title}</b>"]
    if card.employer is not None:
        lines.append(texts.EMPLOYER_LINE.format(value=_clip(card.employer, MAX_TITLE)))
    if card.direction is not None:
        lines.append(
            texts.DIRECTION_LINE.format(
                emoji=texts.DIRECTION_EMOJI[card.direction],
                value=escape(names.directions[card.direction]),
            )
        )
    if card.hiring_countries:
        lines.append(texts.HIRING_LINE.format(value=_countries(card.hiring_countries, names)))
    if card.country is not None:
        lines.append(texts.COUNTRY_LINE.format(value=escape(names.countries[card.country])))
    if card.salary is not None:
        lines.append(texts.SALARY_LINE.format(value=escape(card.salary)))
    matches = [r for r in card.reasons if r.outcome is FactorOutcome.MATCH]
    mismatches = [r for r in card.reasons if r.outcome is FactorOutcome.MISMATCH]
    lines.extend(
        f"{texts.MATCH_MARK} {_reason(r, texts.REASON_MATCH)}" for r in matches[:MAX_MATCH_LINES]
    )
    lines.extend(f"{texts.MISMATCH_MARK} {_reason(r, texts.REASON_MISMATCH)}" for r in mismatches)
    lines.extend(f"{texts.CAVEAT_MARK} {escape(texts.CAVEATS[key])}" for key in card.caveats)
    if card.similar_count:
        lines.append(texts.MORE_SIMILAR.format(count=card.similar_count))
    return "\n".join(lines)


def render_similar(cards: Sequence[Card]) -> str:
    if not cards:
        return texts.SIMILAR_EMPTY
    lines = [texts.SIMILAR_HEADER]
    used = len(lines[0]) + FOOTER_ALLOWANCE
    listed = 0
    for card in cards[:MAX_SIMILAR_LISTED]:
        line = (
            f'{texts.BULLET} <a href="{escape(str(card.url), quote=True)}">'
            f"{_clip(card.title, MAX_SIMILAR_TITLE)}</a>"
        )
        if used + len(line) + 1 > MAX_MESSAGE:
            break
        lines.append(line)
        used += len(line) + 1
        listed += 1
    if listed < len(cards):
        lines.append(texts.SIMILAR_MORE.format(count=len(cards) - listed))
    return "\n".join(lines)


def render_profile(profile: UserProfile, names: Names) -> str:
    stage = profile.career_stage
    directions = profile.directions
    countries = profile.hiring_country
    return "\n".join(
        [
            texts.PROFILE_HEADER,
            texts.PROFILE_STAGE.format(
                value=texts.PROFILE_UNKNOWN if stage is None else texts.STAGE_NAMES[stage.value]
            ),
            texts.PROFILE_DIRECTIONS.format(
                value=texts.PROFILE_UNKNOWN
                if directions is None
                else escape(", ".join(names.directions[key] for key in directions.value))
            ),
            texts.PROFILE_COUNTRIES.format(
                value=texts.PROFILE_UNKNOWN
                if countries is None
                else escape(", ".join(names.countries[code] for code in countries.value))
            ),
        ]
    )


def _countries(codes: Sequence[str], names: Names) -> str:
    listed = ", ".join(escape(names.countries[code]) for code in codes[:MAX_COUNTRIES])
    return listed + texts.ELLIPSIS if len(codes) > MAX_COUNTRIES else listed


def _reason(reason: Reason, labels: dict[str, str]) -> str:
    label = escape(labels[reason.factor])
    if reason.evidence is None:
        return label
    return f"{label}: {_clip(reason.evidence, MAX_EVIDENCE)}"


def _clip(text: str, limit: int) -> str:
    """Escaped, and no longer than `limit` once escaped: markup entities count too."""
    escaped = escape(text)
    if len(escaped) <= limit:
        return escaped
    kept: list[str] = []
    used = len(texts.ELLIPSIS)
    for char in text:
        piece = escape(char)
        if used + len(piece) > limit:
            break
        kept.append(piece)
        used += len(piece)
    return "".join(kept) + texts.ELLIPSIS
