"""Cards, keyboards and the boundary of the bot package. No database, no network."""

import datetime
import uuid
from pathlib import Path

from aiogram.types import InlineKeyboardMarkup
from pydantic import HttpUrl

from seawork.app import Card, Names, Reason
from seawork.bot import keyboards, texts
from seawork.bot.keyboards import CountryCb, DirectionCb, ReactCb, Reaction
from seawork.bot.render import render_card, render_profile, render_similar
from seawork.domain.enums import OpportunityType
from seawork.domain.match import FactorOutcome
from seawork.domain.models import EnrichedOpportunity, NormalizedOpportunity
from seawork.domain.profile import CareerStage, Fact, ProfileFactSource, UserProfile
from seawork.scoring import compute_match_score

NAMES = Names.load(Path("data/reference"))
USER = uuid.UUID(int=1)
NOW = datetime.datetime(2026, 10, 12, 12, 0, tzinfo=datetime.UTC)


def card(**changes: object) -> Card:
    fields: dict[str, object] = {
        "opportunity_id": 7,
        "title": "Chef",
        "employer": "Acme",
        "url": HttpUrl("https://example.com/job/7"),
        "direction": "cruise",
        "salary": "1000 USD / month",
        "reasons": (
            Reason(factor="profession", outcome=FactorOutcome.MATCH, evidence="chef"),
            Reason(factor="country", outcome=FactorOutcome.MISMATCH, evidence=None),
        ),
        "caveats": ("hiring_office_not_normalised",),
        "similar_count": 3,
    }
    fields.update(changes)
    return Card.model_validate(fields)


def test_a_card_shows_title_employer_direction_salary_reasons_and_group() -> None:
    text = render_card(card(), NAMES)

    assert text.splitlines()[0] == "<b>Chef</b>"
    assert "Acme" in text
    assert NAMES.directions["cruise"] in text
    assert "1000 USD / month" in text
    assert f"✓ {texts.REASON_MATCH['profession']}: chef" in text
    assert f"⚠ {texts.REASON_MISMATCH['country']}" in text
    assert texts.CAVEATS["hiring_office_not_normalised"] in text
    assert texts.MORE_SIMILAR.format(count=3) in text


def test_a_card_has_no_percentage() -> None:
    assert "%" not in render_card(card(), NAMES)


def test_everything_that_comes_from_a_foreign_site_is_escaped() -> None:
    hostile = card(
        title="<script>&",
        employer="<i>x</i>",
        reasons=(Reason(factor="profession", outcome=FactorOutcome.MATCH, evidence="<a href=x>"),),
    )

    text = render_card(hostile, NAMES)

    assert "<script>" not in text
    assert "<i>x</i>" not in text
    assert "<a href=x>" not in text
    assert "&lt;script&gt;&amp;" in text


def test_the_worst_card_fits_a_telegram_message() -> None:
    long = "&<" * 300
    worst = card(
        title=long,
        employer=long,
        salary="9" * 100,
        reasons=(
            *(
                Reason(factor=factor, outcome=FactorOutcome.MATCH, evidence=long)
                for factor in ("profession", "direction", "country", "languages")
            ),
            Reason(factor="experience", outcome=FactorOutcome.MISMATCH, evidence=long),
        ),
        caveats=tuple(texts.CAVEATS),
        similar_count=36,
    )

    assert len(render_card(worst, NAMES)) <= 4096


def test_the_worst_similar_list_fits_a_telegram_message() -> None:
    cards = [
        card(
            title="&" * 400,
            url=HttpUrl("https://example.com/" + "a&b" * 300),
            similar_count=0,
        )
        for _ in range(36)
    ]

    text = render_similar(cards)

    assert len(text) <= 4096
    assert texts.SIMILAR_MORE.format(count=36 - text.count("<a href=")) in text


def test_a_long_title_is_cut_without_breaking_an_entity() -> None:
    text = render_card(card(title="&" * 5000), NAMES)

    assert len(text) < 4096
    assert "&amp" in text
    assert "&am…" not in text


def test_only_three_matches_are_listed() -> None:
    reasons = tuple(
        Reason(factor=factor, outcome=FactorOutcome.MATCH, evidence=None)
        for factor in ("profession", "direction", "country", "languages", "experience")
    )

    text = render_card(card(reasons=reasons, caveats=()), NAMES)

    assert text.count("✓") == 3


def test_a_card_without_optional_parts_omits_them() -> None:
    text = render_card(
        card(employer=None, direction=None, salary=None, reasons=(), caveats=(), similar_count=0),
        NAMES,
    )

    assert text == "<b>Chef</b>"


def test_every_factor_the_score_can_name_has_a_line_of_text() -> None:
    opportunity = EnrichedOpportunity(
        normalized=NormalizedOpportunity(
            source_id="padi",
            external_id="1",
            url=HttpUrl("https://example.com/1"),
            title="Chef",
            employer=None,
        ),
        type=OpportunityType.JOB,
        type_confidence=0.9,
        quality_score=1.0,
        quality_flags=[],
    )
    profile = UserProfile(user_id=USER, created_at=NOW, updated_at=NOW)

    factors = {v.factor for v in compute_match_score(profile, opportunity).verdicts}

    assert factors <= texts.REASON_MATCH.keys()
    assert factors <= texts.REASON_MISMATCH.keys()


def test_similar_vacancies_are_links_and_a_long_list_is_cut() -> None:
    cards = [card(title=f"T{i}", similar_count=0) for i in range(25)]

    text = render_similar(cards)

    assert text.count("<a href=") == 20
    assert texts.SIMILAR_MORE.format(count=5) in text
    assert render_similar([]) == texts.SIMILAR_EMPTY


def test_the_profile_says_unknown_for_what_it_does_not_know() -> None:
    profile = UserProfile(
        user_id=USER,
        career_stage=Fact(value=CareerStage.ENTRY, source=ProfileFactSource.STATED, confidence=1.0),
        created_at=NOW,
        updated_at=NOW,
    )

    text = render_profile(profile, NAMES)

    assert texts.STAGE_NAMES[CareerStage.ENTRY] in text
    assert text.count(texts.PROFILE_UNKNOWN) == 2


def test_callback_data_fits_telegram_limit() -> None:
    longest_key = max(NAMES.directions, key=len)
    payloads = [
        ReactCb(opportunity_id=2**63, reaction=Reaction.DISMISS).pack(),
        keyboards.SimilarCb(opportunity_id=2**63).pack(),
        DirectionCb(key=longest_key).pack(),
        CountryCb(key="GB").pack(),
        keyboards.StageCb(stage=CareerStage.EXPERIENCED, onboarding=True).pack(),
    ]

    assert all(len(p.encode()) <= 64 for p in payloads)
    assert ReactCb(opportunity_id=42, reaction=Reaction.SAVE).pack() == "r:42:s"


def labels(markup: InlineKeyboardMarkup) -> list[list[str]]:
    return [[b.text for b in row] for row in markup.inline_keyboard]


def test_a_card_keyboard_has_link_reactions_and_the_group() -> None:
    markup = keyboards.card_keyboard(card())

    assert markup.inline_keyboard[0][0].url == "https://example.com/job/7"
    assert labels(markup)[1] == [texts.INTERESTING, texts.NOT_FOR_ME]
    assert labels(markup)[2] == [texts.SIMILAR.format(count=3)]
    assert len(keyboards.card_keyboard(card(similar_count=0)).inline_keyboard) == 2


def test_a_reaction_replaces_only_the_reaction_row() -> None:
    original = keyboards.card_keyboard(card())

    dismissed = keyboards.reacted(original, 7, Reaction.DISMISS)
    saved = keyboards.reacted(original, 7, Reaction.SAVE)
    reopened = keyboards.reacted(dismissed, 7, Reaction.REOPEN)

    assert labels(dismissed)[1] == [texts.DISMISSED, texts.TAKE_BACK]
    assert labels(saved)[1] == [texts.SAVED]
    assert labels(dismissed)[0] == labels(original)[0]
    assert labels(dismissed)[2] == labels(original)[2]
    assert labels(reopened) == labels(original)


def test_a_picker_keeps_its_selection_in_the_keyboard() -> None:
    options = {"cruise": "Cruise", "yachts": "Yachts"}
    markup = keyboards.picker_keyboard(DirectionCb, options, frozenset(), texts.SKIP)

    assert keyboards.picked(markup, DirectionCb) == frozenset()
    one = keyboards.with_toggled(markup, DirectionCb, "cruise")
    both = keyboards.with_toggled(one, DirectionCb, "yachts")
    back = keyboards.with_toggled(both, DirectionCb, "cruise")

    assert keyboards.picked(one, DirectionCb) == {"cruise"}
    assert keyboards.picked(both, DirectionCb) == {"cruise", "yachts"}
    assert keyboards.picked(back, DirectionCb) == {"yachts"}
    assert labels(back)[-1] == [texts.DONE, texts.SKIP]


def test_only_the_wiring_module_knows_storage_or_the_engine() -> None:
    forbidden = (
        "seawork.storage",
        "seawork.recommendation",
        "seawork.scoring",
        "seawork.ingestion",
        "sqlalchemy",
    )
    for path in Path("src/seawork/bot").glob("*.py"):
        if path.name == "main.py":
            continue
        source = path.read_text()
        assert not [name for name in forbidden if name in source], path
