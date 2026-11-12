"""Cards, keyboards and the boundary of the bot package. No database, no network."""

import datetime
import uuid
from pathlib import Path

from aiogram.types import InlineKeyboardMarkup
from pydantic import HttpUrl

from seawork.app import Card, FeedPage, Names, ReactionState, Reason
from seawork.bot import keyboards, texts
from seawork.bot.keyboards import CountryCb, DirectionCb, ReactCb, Reaction
from seawork.bot.render import render_card, render_feed, render_profile, render_similar
from seawork.domain.enums import OpportunityType
from seawork.domain.match import FactorOutcome
from seawork.domain.models import EnrichedOpportunity, NormalizedOpportunity
from seawork.domain.profile import CareerStage, Fact, ProfileFactSource, UserProfile
from seawork.ingestion.references import load_yaml
from seawork.scoring import compute_match_score

REFERENCE = Path("data/reference")
NAMES = Names.load(REFERENCE)
USER = uuid.UUID(int=1)
NOW = datetime.datetime(2026, 10, 12, 12, 0, tzinfo=datetime.UTC)


def card(**changes: object) -> Card:
    fields: dict[str, object] = {
        "opportunity_id": 7,
        "title": "Chef",
        "employer": "Acme",
        "url": HttpUrl("https://example.com/job/7"),
        "profession": None,
        "direction": "cruise",
        "country": None,
        "hiring_countries": (),
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
    assert "🏢 Acme" in text
    assert f"🚢 {NAMES.directions['cruise']}" in text
    assert "💰 1000 USD / month" in text
    assert f"✅ {texts.REASON_MATCH['profession']}: chef" in text
    assert f"⚠️ {texts.REASON_MISMATCH['country']}" in text
    assert f"{texts.CAVEAT_MARK} {texts.CAVEATS['hiring_office_not_normalised']}" in text
    assert texts.MORE_SIMILAR.format(count=3) in text


def test_the_profession_emoji_is_shown_only_when_one_is_defined() -> None:
    assert render_card(card(profession="galley"), NAMES).startswith("<b>🍳 Chef</b>")
    assert render_card(card(profession="master"), NAMES).startswith("<b>Chef</b>")
    assert render_card(card(profession=None), NAMES).startswith("<b>Chef</b>")


def test_offices_and_the_country_of_work_are_named() -> None:
    text = render_card(card(hiring_countries=("GB", "PH"), country="PH"), NAMES)

    assert f"🌍 Офис найма: {NAMES.countries['GB']}, {NAMES.countries['PH']}" in text
    assert f"📍 Страна работы: {NAMES.countries['PH']}" in text


def test_a_long_list_of_offices_is_cut() -> None:
    codes = tuple(NAMES.countries)[:6]

    text = render_card(card(hiring_countries=codes), NAMES)

    assert NAMES.countries[codes[2]] in text
    assert NAMES.countries[codes[3]] not in text
    assert f"{NAMES.countries[codes[2]]}{texts.ELLIPSIS}" in text


def test_every_emoji_key_exists_in_the_reference() -> None:
    professions = {row["key"] for row in load_yaml(REFERENCE / "professions.yaml")["professions"]}

    assert texts.PROFESSION_EMOJI.keys() <= professions
    assert texts.DIRECTION_EMOJI.keys() == NAMES.directions.keys()


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
        profession="galley",
        hiring_countries=tuple(NAMES.countries),
        country="PH",
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

    assert text.count("✅") == 3


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
        ReactCb(opportunity_id=2**63, reaction=Reaction.DISMISS, index=4).pack(),
        keyboards.SimilarCb(opportunity_id=2**63, index=4).pack(),
        keyboards.PageCb(index=4).pack(),
        DirectionCb(key=longest_key, onboarding=True).pack(),
        CountryCb(key="GB", onboarding=True).pack(),
        keyboards.StageCb(stage=CareerStage.EXPERIENCED, onboarding=True).pack(),
        *(keyboards.NavCb(to=nav).pack() for nav in keyboards.Nav),
        *(keyboards.ChangeCb(what=what).pack() for what in keyboards.Change),
    ]

    assert all(len(p.encode()) <= 64 for p in payloads)
    assert ReactCb(opportunity_id=42, reaction=Reaction.SAVE, index=3).pack() == "r:42:s:3"
    assert keyboards.PageCb(index=2).pack() == "p:2"


def labels(markup: InlineKeyboardMarkup) -> list[list[str]]:
    return [[b.text for b in row] for row in markup.inline_keyboard]


def page(**changes: object) -> FeedPage:
    fields: dict[str, object] = {
        "index": 1,
        "total": 3,
        "card": card(),
        "fresh": None,
        "reaction": ReactionState.NONE,
        "has_prev": True,
        "has_next": True,
    }
    fields.update(changes)
    return FeedPage.model_validate(fields)


def test_a_page_keyboard_has_link_reactions_navigation_and_menu() -> None:
    markup = keyboards.page_keyboard(page())

    assert markup.inline_keyboard[0][0].url == "https://example.com/job/7"
    assert labels(markup)[1:] == [
        [texts.INTERESTING, texts.NOT_FOR_ME],
        [texts.PREVIOUS, texts.SIMILAR, texts.NEXT],
        [texts.BACK_TO_MENU],
    ]
    assert markup.inline_keyboard[2][0].callback_data == "p:0"
    assert markup.inline_keyboard[2][2].callback_data == "p:2"


def test_the_first_and_the_last_card_have_one_arrow_only() -> None:
    first = labels(keyboards.page_keyboard(page(index=0, has_prev=False)))
    last = labels(
        keyboards.page_keyboard(page(index=2, has_next=False, card=card(similar_count=0)))
    )

    assert first[2] == [texts.SIMILAR, texts.NEXT]
    assert last[2] == [texts.PREVIOUS]


def test_a_lone_card_without_group_has_no_navigation_row() -> None:
    lone = page(has_prev=False, has_next=False, card=card(similar_count=0))

    assert labels(keyboards.page_keyboard(lone)) == [
        [texts.OPEN_VACANCY],
        [texts.INTERESTING, texts.NOT_FOR_ME],
        [texts.BACK_TO_MENU],
    ]


def test_the_reaction_row_follows_the_state() -> None:
    saved = labels(keyboards.page_keyboard(page(reaction=ReactionState.SAVED)))[1]
    dismissed = labels(keyboards.page_keyboard(page(reaction=ReactionState.DISMISSED)))[1]

    assert saved == [texts.SAVED]
    assert dismissed == [texts.DISMISSED, texts.TAKE_BACK]


def test_the_end_of_the_carousel_offers_the_way_back_and_the_menu() -> None:
    end = page(card=None, index=3, total=3, has_next=False)

    assert render_feed(end, NAMES) == texts.FEED_DONE
    assert labels(keyboards.page_keyboard(end)) == [[texts.PREVIOUS], [texts.BACK_TO_MENU]]
    assert render_feed(
        page(card=None, index=0, total=0, has_prev=False, has_next=False), NAMES
    ) == (texts.EMPTY_FEED)


def test_a_page_says_which_card_of_how_many() -> None:
    text = render_feed(page(index=1, total=5), NAMES)

    assert text.startswith(f"{texts.FEED_HEADER.format(position=2, total=5)}\n\n<b>Chef</b>")


def test_the_menu_shows_the_state_of_notifications() -> None:
    on = labels(keyboards.menu_keyboard(notifications_on=True))
    off = labels(keyboards.menu_keyboard(notifications_on=False))

    assert [row[0] for row in on] == [
        texts.MENU_FEED,
        texts.MENU_PROFILE,
        texts.MENU_NOTIFY_ON,
        texts.MENU_DATA,
        texts.MENU_HELP,
    ]
    assert off[2] == [texts.MENU_NOTIFY_OFF]


def test_a_picker_keeps_its_selection_in_the_keyboard() -> None:
    options = {"cruise": "Cruise", "yachts": "Yachts"}
    markup = keyboards.picker_keyboard(
        DirectionCb, options, frozenset(), texts.SKIP, onboarding=True
    )

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
