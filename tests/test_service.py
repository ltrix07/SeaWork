"""Use cases of the service layer, on a real database and with no messenger in sight.

Follows test_profile.py: its own schema, skipped when no PostgreSQL is reachable.
The card tests at the top need no database.
"""

import datetime
import hashlib
import threading
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import HttpUrl
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from seawork.app import DAILY_LIMIT, FeedPage, Names, ReactionState, SeaWorkService
from seawork.app.cards import card_from
from seawork.domain.enums import OpportunityStatus, OpportunityType, Provenance, SourceTrust
from seawork.domain.inferred import Inferred
from seawork.domain.match import FactorOutcome, FactorVerdict, MatchScore
from seawork.domain.models import (
    EnrichedOpportunity,
    HiringScope,
    NormalizedOpportunity,
    SalaryRange,
)
from seawork.domain.profile import BehaviourAction, CareerStage, Fact, ProfileFactSource
from seawork.domain.recommendation import Candidate
from seawork.ingestion.base import RawItem
from seawork.storage.account_repository import AccountRepository
from seawork.storage.models import (
    Base,
    BehaviourEventRecord,
    ChannelAccountRecord,
    DeliveryRecord,
    OpportunityRecord,
    UserProfileRecord,
)
from seawork.storage.profile_repository import ProfileRepository
from seawork.storage.profile_vocabulary import ProfileVocabulary, UnknownReferenceKeyError
from seawork.storage.repository import Repository

VOCABULARY = ProfileVocabulary.from_directory(Path("data/reference"))

NOW = datetime.datetime(2026, 10, 12, 12, 0, tzinfo=datetime.UTC)
CHANNEL = "telegram"


# --- cards ----------------------------------------------------------------------


def verdict(
    factor: str,
    outcome: FactorOutcome,
    weight: float,
    evidence: str | None = None,
    caveats: list[str] | None = None,
) -> FactorVerdict:
    return FactorVerdict(
        factor=factor,
        outcome=outcome,
        weight=weight,
        opportunity_evidence=evidence,
        caveats=caveats or [],
    )


def score(*verdicts: FactorVerdict) -> MatchScore:
    return MatchScore(
        value=0.5,
        comparable_factors=len(verdicts),
        verdicts=list(verdicts),
        computed_at=NOW,
        profile_fingerprint="p",
        opportunity_fingerprint="o",
    )


def plain_candidate(salary: SalaryRange | None = None) -> Candidate:
    return Candidate(
        opportunity_id=7,
        opportunity=EnrichedOpportunity(
            normalized=NormalizedOpportunity(
                source_id="padi",
                external_id="7",
                url=HttpUrl("https://example.com/job/7"),
                title="Chef",
                employer="Acme",
            ),
            type=OpportunityType.JOB,
            type_confidence=0.9,
            direction=Inferred(value="cruise", provenance=Provenance.RULE, confidence=0.8),
            salary=None
            if salary is None
            else Inferred(value=salary, provenance=Provenance.RULE, confidence=0.8),
            quality_score=1.0,
            quality_flags=[],
        ),
        employer="Acme",
        freshness=NOW,
    )


def test_a_card_shows_matches_first_and_only_the_heaviest_mismatch() -> None:
    card = card_from(
        plain_candidate(),
        score(
            verdict("country", FactorOutcome.MISMATCH, 0.4, "Spain"),
            verdict("direction", FactorOutcome.MATCH, 0.3, "cruise"),
            verdict("languages", FactorOutcome.MISMATCH, 0.9, "Dutch"),
            verdict("profession", FactorOutcome.MATCH, 0.8, "chef"),
            verdict("certificates", FactorOutcome.NOT_COMPARABLE, 0.0, "STCW"),
        ),
        similar_count=3,
    )

    assert [(r.factor, r.outcome) for r in card.reasons] == [
        ("profession", FactorOutcome.MATCH),
        ("direction", FactorOutcome.MATCH),
        ("languages", FactorOutcome.MISMATCH),
    ]
    assert card.reasons[2].evidence == "Dutch"
    assert (card.opportunity_id, card.title, card.employer) == (7, "Chef", "Acme")
    assert card.direction == "cruise"
    assert card.similar_count == 3


def test_caveats_of_every_verdict_are_kept_once() -> None:
    card = card_from(
        plain_candidate(),
        score(
            verdict("hiring_country", FactorOutcome.MATCH, 0.5, caveats=["hiring_office_x"]),
            verdict("certificates", FactorOutcome.NOT_COMPARABLE, 0.0, caveats=["training", "a"]),
            verdict("goal", FactorOutcome.MATCH, 0.5, caveats=["training"]),
        ),
        similar_count=0,
    )

    assert card.caveats == ("hiring_office_x", "training", "a")


@pytest.mark.parametrize(
    ("salary", "expected"),
    [
        (
            SalaryRange(
                minimum=Decimal("1500.00"), maximum=Decimal(2000), currency="USD", period="month"
            ),
            "1500-2000 USD / month",
        ),
        (SalaryRange(minimum=Decimal(1500), currency="EUR"), ">= 1500 EUR"),
        (
            SalaryRange(maximum=Decimal("2000.5"), currency="EUR", period="week"),
            "<= 2000.5 EUR / week",
        ),
    ],
)
def test_salary_is_formatted_without_trailing_zeros(salary: SalaryRange, expected: str) -> None:
    assert card_from(plain_candidate(salary), score(), 0).salary == expected


def test_no_salary_is_none() -> None:
    assert card_from(plain_candidate(), score(), 0).salary is None


def test_a_salary_without_an_amount_is_none() -> None:
    salary = SalaryRange(currency="GBP")

    assert card_from(plain_candidate(salary), score(), 0).salary is None


def test_the_service_layer_does_not_import_a_bot_framework() -> None:
    sources = Path("src/seawork/app").glob("*.py")
    assert all("aiogram" not in path.read_text() for path in sources)


# --- database -------------------------------------------------------------------


@pytest.fixture
def test_schema() -> str:
    return "seawork_service_test"


@pytest.fixture
def sessions(session: Session) -> sessionmaker[Session]:
    return sessionmaker(bind=session.get_bind())


@pytest.fixture
def service(sessions: sessionmaker[Session]) -> SeaWorkService:
    return SeaWorkService(sessions, VOCABULARY, Names.load(Path("data/reference")))


def store(
    session: Session,
    external_id: str,
    employer: str | None = None,
    profession: str = "chef",
    countries: list[str] | None = None,
    status: OpportunityStatus = OpportunityStatus.ACTIVE,
    title: str | None = None,
) -> int:
    payload = external_id
    raw = Repository(session).save_raw(
        RawItem(
            source_id="padi",
            external_id=external_id,
            url=HttpUrl(f"https://example.com/job/{external_id}"),
            fetched_at=NOW,
            payload=payload,
            content_type="application/xml",
            content_hash=hashlib.sha256(payload.encode()).hexdigest(),
            http_status=200,
        )
    )
    stored = EnrichedOpportunity(
        normalized=NormalizedOpportunity(
            source_id="padi",
            external_id=external_id,
            url=HttpUrl(f"https://example.com/job/{external_id}"),
            title=title or f"Vacancy {external_id}",
            employer=employer or f"Employer {external_id}",
        ),
        type=OpportunityType.JOB,
        type_confidence=0.9,
        profession=Inferred(value=profession, provenance=Provenance.RULE, confidence=0.8),
        hiring_scope=None
        if countries is None
        else Inferred(
            value=HiringScope(countries=countries), provenance=Provenance.RULE, confidence=0.8
        ),
        quality_score=1.0,
        quality_flags=[],
    )
    Repository(session).upsert_opportunity(
        raw.id, stored, SourceTrust.PRIMARY, status, keep_closed=False
    )
    return session.execute(
        select(OpportunityRecord.id).where(OpportunityRecord.external_id == external_id)
    ).scalar_one()


def count(session: Session, record: type[Base]) -> int:
    session.rollback()
    return session.execute(select(func.count()).select_from(record)).scalar_one()


def onboarded(service: SeaWorkService, external_id: str = "100") -> uuid.UUID:
    return service.start(CHANNEL, external_id).user_id


def test_the_first_start_creates_a_profile_and_an_account(
    service: SeaWorkService, session: Session
) -> None:
    result = service.start(CHANNEL, "100")

    assert result.is_new
    assert not result.stage_known
    assert count(session, UserProfileRecord) == 1
    account = session.execute(select(ChannelAccountRecord)).scalar_one()
    assert (account.channel, account.external_id, account.user_id) == (
        CHANNEL,
        "100",
        result.user_id,
    )


def test_a_repeated_start_finds_the_same_person(service: SeaWorkService, session: Session) -> None:
    first = service.start(CHANNEL, "100")
    second = service.start(CHANNEL, "100")

    assert not second.is_new
    assert second.user_id == first.user_id
    assert count(session, UserProfileRecord) == 1
    assert count(session, ChannelAccountRecord) == 1


def test_the_same_id_in_another_channel_is_another_account(service: SeaWorkService) -> None:
    assert service.start("telegram", "100").user_id != service.start("web", "100").user_id


def test_simultaneous_first_starts_make_one_person(
    service: SeaWorkService, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    workers = 8
    barrier = threading.Barrier(workers, timeout=10)
    results: list[tuple[uuid.UUID, bool]] = []
    failures: list[BaseException] = []
    rollbacks: list[None] = []
    original_find = AccountRepository.find
    original_rollback = Session.rollback

    # Every worker looks, finds nobody, and only then moves on to the insert: all of
    # them reach the unique index together and all but one must lose there.
    def find_then_wait(self: AccountRepository, channel: str, external_id: str) -> uuid.UUID | None:
        found = original_find(self, channel, external_id)
        barrier.wait()
        return found

    def counted_rollback(self: Session) -> None:
        rollbacks.append(None)
        original_rollback(self)

    monkeypatch.setattr(AccountRepository, "find", find_then_wait)
    monkeypatch.setattr(Session, "rollback", counted_rollback)

    def first_message() -> None:
        try:
            result = service.start(CHANNEL, "100")
            results.append((result.user_id, result.is_new))
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=first_message) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures
    assert len(rollbacks) == workers - 1
    assert len({user_id for user_id, _ in results}) == 1
    assert [is_new for _, is_new in results].count(True) == 1
    assert count(session, UserProfileRecord) == 1
    assert count(session, ChannelAccountRecord) == 1


def test_a_lost_race_leaves_no_orphan_profile(
    sessions: sessionmaker[Session], session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    with sessions() as winner_session:
        winner, _ = AccountRepository(winner_session).find_or_create(CHANNEL, "100")

    # The loser looked before the winner committed: its first lookup finds nothing,
    # so it reaches the insert and meets the unique index.
    original = AccountRepository.find
    lookups = iter([None])

    def late_lookup(self: AccountRepository, channel: str, external_id: str) -> uuid.UUID | None:
        for stale in lookups:
            return stale
        return original(self, channel, external_id)

    monkeypatch.setattr(AccountRepository, "find", late_lookup)
    with sessions() as loser_session:
        user_id, is_new = AccountRepository(loser_session).find_or_create(CHANNEL, "100")

    assert (user_id, is_new) == (winner, False)
    assert count(session, UserProfileRecord) == 1
    assert count(session, ChannelAccountRecord) == 1


def test_an_answer_is_stored_as_stated_and_known_on_the_next_start(
    service: SeaWorkService,
) -> None:
    user_id = onboarded(service)
    service.answer_stage(user_id, CareerStage.ENTRY, onboarding=True)

    assert service.start(CHANNEL, "100").stage_known
    profile = service.export(user_id)
    assert profile is not None
    fact = profile.profile.career_stage
    assert fact is not None
    assert (fact.value, fact.source, fact.confidence) == (
        CareerStage.ENTRY,
        ProfileFactSource.STATED,
        1.0,
    )


def test_directions_are_checked_against_the_reference(service: SeaWorkService) -> None:
    user_id = onboarded(service)

    service.set_directions(user_id, ["cruise", "diving"])
    with pytest.raises(UnknownReferenceKeyError):
        service.set_directions(user_id, ["nonsense"])

    exported = service.export(user_id)
    assert exported is not None
    assert exported.profile.directions is not None
    assert exported.profile.directions.value == ["cruise", "diving"]


def test_hiring_countries_are_stored_as_stated(service: SeaWorkService) -> None:
    user_id = onboarded(service)
    service.set_hiring_countries(user_id, ["ph"])

    exported = service.export(user_id)
    assert exported is not None
    assert exported.profile.hiring_country is not None
    assert exported.profile.hiring_country.value == ["PH"]


def test_hiring_country_options_follow_frequency_and_skip_closed(
    service: SeaWorkService, session: Session
) -> None:
    store(session, "1", countries=["PH", "IN"])
    store(session, "2", countries=["PH"])
    store(session, "3", countries=["IN", "PH", "PH"])
    store(session, "4", countries=["ID"])
    store(session, "5", countries=["ID"], status=OpportunityStatus.REJECTED)
    store(session, "6")

    assert service.hiring_country_options() == ["PH", "IN", "ID"]


def test_hiring_country_options_are_capped_at_eight(
    service: SeaWorkService, session: Session
) -> None:
    codes = ["GB", "US", "NL", "DE", "PL", "FR", "ES", "IT", "IN", "ID"]
    store(session, "1", countries=codes)

    assert len(service.hiring_country_options()) == 8


@pytest.mark.parametrize(
    ("stage", "onboarding", "show_batch", "ask_directions"),
    [
        (CareerStage.ENTRY, True, True, True),
        (CareerStage.WORKING, True, True, False),
        (CareerStage.UNDECIDED, True, True, False),
        (CareerStage.ENTRY, False, False, False),
        (CareerStage.WORKING, False, False, False),
    ],
)
def test_the_service_decides_what_follows_a_stage_answer(
    service: SeaWorkService,
    stage: CareerStage,
    onboarding: bool,
    show_batch: bool,
    ask_directions: bool,
) -> None:
    answered = service.answer_stage(onboarded(service), stage, onboarding=onboarding)

    assert (answered.show_batch, answered.ask_directions) == (show_batch, ask_directions)


def test_directions_are_not_asked_again_when_known(service: SeaWorkService) -> None:
    user_id = onboarded(service)
    service.set_directions(user_id, ["cruise"])

    answered = service.answer_stage(user_id, CareerStage.ENTRY, onboarding=True)

    assert answered.show_batch
    assert not answered.ask_directions


def test_touch_finds_without_creating_and_unblocks(
    service: SeaWorkService, session: Session
) -> None:
    assert service.touch(CHANNEL, "100") is None
    assert count(session, UserProfileRecord) == 0
    user_id = onboarded(service)
    service.mark_blocked(CHANNEL, "100", NOW)

    assert service.touch(CHANNEL, "100") == user_id
    assert service.digest_recipients(CHANNEL) == [("100", user_id)]


def test_the_names_come_from_the_reference_files(service: SeaWorkService) -> None:
    names = service.names()

    assert names.directions["cruise"]
    assert names.countries["GB"] == "United Kingdom"


def test_prepare_batch_writes_nothing(service: SeaWorkService, session: Session) -> None:
    for number in range(3):
        store(session, str(number))
    user_id = onboarded(service)

    first = service.prepare_batch(user_id, NOW)
    second = service.prepare_batch(user_id, NOW)

    assert len(first) == 3
    assert [p.card.opportunity_id for p in first] == [p.card.opportunity_id for p in second]
    assert count(session, DeliveryRecord) == 0
    assert count(session, BehaviourEventRecord) == 0


def test_confirm_delivered_writes_exactly_what_was_passed(
    service: SeaWorkService, session: Session
) -> None:
    for number in range(4):
        store(session, str(number))
    user_id = onboarded(service)

    prepared = service.prepare_batch(user_id, NOW)
    sent = [p.recommendation for p in prepared[:2]]
    service.confirm_delivered(user_id, sent, CHANNEL, NOW)

    exported = service.export(user_id)
    assert exported is not None
    assert [
        (d.opportunity_id, d.similar_ids, d.channel, d.delivered_at) for d in exported.deliveries
    ] == [(r.opportunity_id, r.similar_ids, CHANNEL, NOW) for r in sent]
    delivered = {d.opportunity_id for d in exported.deliveries}
    unsent = {p.card.opportunity_id for p in prepared[2:]}
    again = {p.card.opportunity_id for p in service.prepare_batch(user_id, NOW)}
    assert unsent <= again
    assert not delivered & again


def test_a_group_is_delivered_with_its_members_and_they_do_not_come_back(
    service: SeaWorkService, session: Session
) -> None:
    first = store(session, "1", employer="Acme", title="Chef")
    second = store(session, "2", employer="Acme", title="Chef")
    user_id = onboarded(service)

    prepared = service.prepare_batch(user_id, NOW)
    assert len(prepared) == 1
    assert prepared[0].card.similar_count == 1
    service.confirm_delivered(user_id, [prepared[0].recommendation], CHANNEL, NOW)

    exported = service.export(user_id)
    assert exported is not None
    assert {first, second} == {
        exported.deliveries[0].opportunity_id,
        *exported.deliveries[0].similar_ids,
    }
    assert service.prepare_batch(user_id, NOW) == []


def test_the_daily_limit_is_shared_by_every_call_of_the_day(
    service: SeaWorkService, session: Session
) -> None:
    for number in range(DAILY_LIMIT + 3):
        store(session, str(number))
    user_id = onboarded(service)

    morning = service.prepare_batch(user_id, NOW)
    assert len(morning) == DAILY_LIMIT
    service.confirm_delivered(user_id, [p.recommendation for p in morning[:2]], CHANNEL, NOW)

    afternoon = service.prepare_batch(user_id, NOW + datetime.timedelta(hours=5))
    assert len(afternoon) == DAILY_LIMIT - 2
    service.confirm_delivered(user_id, [p.recommendation for p in afternoon], CHANNEL, NOW)

    assert service.prepare_batch(user_id, NOW + datetime.timedelta(hours=8)) == []


def test_the_day_is_a_utc_day(service: SeaWorkService, session: Session) -> None:
    for number in range(DAILY_LIMIT + 2):
        store(session, str(number))
    user_id = onboarded(service)
    late = datetime.datetime(2026, 10, 12, 23, 59, 59, tzinfo=datetime.UTC)
    service.confirm_delivered(
        user_id, [p.recommendation for p in service.prepare_batch(user_id, late)], CHANNEL, late
    )

    just_after = datetime.datetime(2026, 10, 13, 0, 0, tzinfo=datetime.UTC)
    # 01:00 in UTC+3 is 22:00 UTC of the day before: still the exhausted day.
    same_day_elsewhere = datetime.datetime(
        2026, 10, 13, 1, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=3))
    )

    assert service.prepare_batch(user_id, same_day_elsewhere) == []
    assert len(service.prepare_batch(user_id, just_after)) == 2


def test_dismissed_stays_out_until_reopened(service: SeaWorkService, session: Session) -> None:
    only = store(session, "1")
    user_id = onboarded(service)

    service.react(user_id, only, BehaviourAction.DISMISSED, NOW)
    assert service.prepare_batch(user_id, NOW) == []

    service.react(user_id, only, BehaviourAction.REOPENED, NOW + datetime.timedelta(minutes=1))
    assert [p.card.opportunity_id for p in service.prepare_batch(user_id, NOW)] == [only]


def test_saving_is_journalled_and_does_not_hide_the_vacancy(
    service: SeaWorkService, session: Session
) -> None:
    only = store(session, "1")
    user_id = onboarded(service)

    service.react(user_id, only, BehaviourAction.SAVED, NOW)

    assert count(session, BehaviourEventRecord) == 1
    assert len(service.prepare_batch(user_id, NOW)) == 1


@pytest.mark.parametrize("action", [BehaviourAction.VIEWED, BehaviourAction.APPLIED])
def test_only_reactions_to_a_card_are_accepted(
    service: SeaWorkService, session: Session, action: BehaviourAction
) -> None:
    user_id = onboarded(service)

    with pytest.raises(ValueError):
        service.react(user_id, 1, action, NOW)
    assert count(session, BehaviourEventRecord) == 0


def test_similar_returns_the_members_of_a_delivered_group(
    service: SeaWorkService, session: Session
) -> None:
    members = {store(session, str(n), employer="Acme", title="Chef") for n in range(3)}
    user_id = onboarded(service)
    prepared = service.prepare_batch(user_id, NOW)
    service.confirm_delivered(user_id, [prepared[0].recommendation], CHANNEL, NOW)

    cards = service.similar(user_id, prepared[0].card.opportunity_id)

    assert {card.opportunity_id for card in cards} == members - {prepared[0].card.opportunity_id}
    assert all(card.similar_count == 0 for card in cards)


def test_similar_of_something_never_delivered_is_an_error(service: SeaWorkService) -> None:
    user_id = onboarded(service)

    with pytest.raises(LookupError):
        service.similar(user_id, 1)


def test_pause_and_block_remove_a_person_from_the_digest(service: SeaWorkService) -> None:
    paused = onboarded(service, "1")
    blocked = onboarded(service, "2")
    onboarded(service, "3")

    service.pause(paused, NOW)
    service.mark_blocked(CHANNEL, "2", NOW)

    assert [external for external, _ in service.digest_recipients(CHANNEL)] == ["3"]

    service.resume(paused)
    assert {external for external, _ in service.digest_recipients(CHANNEL)} == {"1", "3"}

    service.start(CHANNEL, "2")
    assert ("2", blocked) in service.digest_recipients(CHANNEL)


def test_the_digest_lists_only_its_own_channel(service: SeaWorkService) -> None:
    onboarded(service, "1")
    service.start("web", "2")

    assert [external for external, _ in service.digest_recipients(CHANNEL)] == ["1"]


def test_blocking_and_asking_are_stamped_once(service: SeaWorkService, session: Session) -> None:
    user_id = onboarded(service)

    service.mark_blocked(CHANNEL, "100", NOW)
    service.mark_blocked(CHANNEL, "100", NOW + datetime.timedelta(days=1))
    service.mark_hiring_country_asked(user_id, NOW)
    service.mark_hiring_country_asked(user_id, NOW + datetime.timedelta(days=1))

    session.rollback()
    account = session.execute(select(ChannelAccountRecord)).scalar_one()
    assert account.blocked_at == NOW
    assert account.hiring_country_asked_at == NOW


def test_delete_removes_the_person_and_everything_attached(
    service: SeaWorkService, session: Session
) -> None:
    only = store(session, "1")
    user_id = onboarded(service)
    prepared = service.prepare_batch(user_id, NOW)
    service.confirm_delivered(user_id, [prepared[0].recommendation], CHANNEL, NOW)
    service.react(user_id, only, BehaviourAction.SAVED, NOW)

    assert service.delete(user_id)

    assert service.export(user_id) is None
    for record in (UserProfileRecord, ChannelAccountRecord, DeliveryRecord, BehaviourEventRecord):
        assert count(session, record) == 0
    assert not service.delete(user_id)


def test_export_carries_the_profile_the_journal_and_the_deliveries(
    service: SeaWorkService, session: Session
) -> None:
    only = store(session, "1")
    user_id = onboarded(service)
    service.react(user_id, only, BehaviourAction.SAVED, NOW)
    prepared = service.prepare_batch(user_id, NOW)
    service.confirm_delivered(user_id, [prepared[0].recommendation], CHANNEL, NOW)

    exported = service.export(user_id)

    assert exported is not None
    assert exported.profile.user_id == user_id
    assert len(exported.events) == 1
    assert len(exported.deliveries) == 1


def test_export_carries_the_channel_accounts(service: SeaWorkService) -> None:
    user_id = onboarded(service, "555")
    service.mark_blocked(CHANNEL, "555", NOW)

    exported = service.export(user_id)

    assert exported is not None
    [account] = exported.accounts
    assert (account.channel, account.external_id, account.blocked_at) == (CHANNEL, "555", NOW)
    assert account.paused_at is None
    assert account.hiring_country_asked_at is None


def test_start_does_not_touch_an_unblocked_account(
    service: SeaWorkService, session: Session
) -> None:
    onboarded(service)

    service.start(CHANNEL, "100")

    session.rollback()
    assert session.execute(select(ChannelAccountRecord.blocked_at)).scalar_one() is None


# --- should_ask_hiring_country --------------------------------------------------


def delivered_yesterday(service: SeaWorkService, session: Session) -> uuid.UUID:
    store(session, "1")
    user_id = onboarded(service)
    yesterday = NOW - datetime.timedelta(days=1)
    prepared = service.prepare_batch(user_id, yesterday)
    service.confirm_delivered(user_id, [p.recommendation for p in prepared], CHANNEL, yesterday)
    return user_id


def test_the_question_is_due_on_the_second_day(service: SeaWorkService, session: Session) -> None:
    user_id = delivered_yesterday(service, session)

    assert service.should_ask_hiring_country(user_id, NOW)


def test_the_question_waits_until_something_was_delivered(service: SeaWorkService) -> None:
    user_id = onboarded(service)

    assert not service.should_ask_hiring_country(user_id, NOW)


def test_the_question_is_not_asked_on_the_day_of_the_first_delivery(
    service: SeaWorkService, session: Session
) -> None:
    user_id = delivered_yesterday(service, session)
    same_day = NOW - datetime.timedelta(days=1) + datetime.timedelta(hours=6)

    assert not service.should_ask_hiring_country(user_id, same_day)


def test_the_question_is_skipped_when_the_country_is_known(
    service: SeaWorkService, session: Session
) -> None:
    user_id = delivered_yesterday(service, session)
    service.set_hiring_countries(user_id, ["PH"])

    assert not service.should_ask_hiring_country(user_id, NOW)


def test_an_empty_answer_is_still_an_answer(service: SeaWorkService, session: Session) -> None:
    user_id = delivered_yesterday(service, session)
    service.set_hiring_countries(user_id, [])

    assert not service.should_ask_hiring_country(user_id, NOW)


def test_the_question_is_asked_once(service: SeaWorkService, session: Session) -> None:
    user_id = delivered_yesterday(service, session)
    service.mark_hiring_country_asked(user_id, NOW)

    assert not service.should_ask_hiring_country(user_id, NOW + datetime.timedelta(days=1))


# --- carousel --------------------------------------------------------------------


def shown(
    service: SeaWorkService, user_id: uuid.UUID, index: int, now: datetime.datetime = NOW
) -> FeedPage:
    page = service.feed_page(user_id, index, now, entering=False)
    if page.fresh is not None:
        service.confirm_delivered(user_id, [page.fresh], CHANNEL, now)
    return page


def test_a_card_carries_profession_country_and_offices() -> None:
    candidate = plain_candidate()
    opportunity = candidate.opportunity.model_copy(
        update={
            "profession": Inferred(value="galley", provenance=Provenance.RULE, confidence=0.8),
            "country": Inferred(value="PH", provenance=Provenance.RULE, confidence=0.8),
            "hiring_scope": Inferred(
                value=HiringScope(countries=["GB", "PH"]),
                provenance=Provenance.RULE,
                confidence=0.8,
            ),
        }
    )

    card = card_from(candidate.model_copy(update={"opportunity": opportunity}), score(), 0)

    assert (card.profession, card.country, card.hiring_countries) == ("galley", "PH", ("GB", "PH"))
    plain = card_from(candidate, score(), 0)
    assert (plain.profession, plain.country, plain.hiring_countries) == (None, None, ())


def test_a_new_card_is_offered_at_the_next_position_and_prepared_nothing_is_written(
    service: SeaWorkService, session: Session
) -> None:
    for number in range(3):
        store(session, str(number))
    user_id = onboarded(service)

    page = service.feed_page(user_id, 0, NOW, entering=False)

    assert (page.index, page.total, page.has_prev, page.has_next) == (0, 3, False, True)
    assert page.card is not None
    assert page.fresh is not None
    assert page.fresh.opportunity_id == page.card.opportunity_id
    assert count(session, DeliveryRecord) == 0


def test_a_delivered_card_comes_back_at_its_position_without_a_new_delivery(
    service: SeaWorkService, session: Session
) -> None:
    for number in range(3):
        store(session, str(number))
    user_id = onboarded(service)
    first = shown(service, user_id, 0)
    second = shown(service, user_id, 1)

    again = service.feed_page(user_id, 0, NOW, entering=False)

    assert again.card == first.card
    assert again.fresh is None
    assert (again.total, again.has_prev, again.has_next) == (3, False, True)
    assert service.feed_page(user_id, 1, NOW, entering=False).card == second.card
    assert count(session, DeliveryRecord) == 2


def test_the_page_after_the_last_delivery_is_the_next_new_card_then_the_end(
    service: SeaWorkService, session: Session
) -> None:
    for number in range(2):
        store(session, str(number))
    user_id = onboarded(service)
    shown(service, user_id, 0)
    last = shown(service, user_id, 1)

    end = service.feed_page(user_id, 2, NOW, entering=False)

    assert (last.index, last.has_next) == (1, False)
    assert (end.card, end.fresh, end.total) == (None, None, 2)
    assert (end.index, end.has_prev, end.has_next) == (2, True, False)
    assert service.feed_page(user_id, 9, NOW, entering=False).index == 2


def test_nothing_to_show_is_an_empty_page(service: SeaWorkService) -> None:
    user_id = onboarded(service)

    page = service.feed_page(user_id, 0, NOW, entering=False)

    assert (page.card, page.total, page.has_prev, page.has_next) == (None, 0, False, False)


def test_paging_never_passes_the_daily_limit(service: SeaWorkService, session: Session) -> None:
    for number in range(DAILY_LIMIT + 3):
        store(session, str(number))
    user_id = onboarded(service)

    pages = [shown(service, user_id, index) for index in range(DAILY_LIMIT + 2)]

    assert [p.card is not None for p in pages] == [True] * DAILY_LIMIT + [False] * 2
    assert pages[0].total == DAILY_LIMIT
    assert not pages[DAILY_LIMIT - 1].has_next
    assert count(session, DeliveryRecord) == DAILY_LIMIT


def test_the_limit_counts_what_other_calls_of_the_day_delivered(
    service: SeaWorkService, session: Session
) -> None:
    for number in range(DAILY_LIMIT + 2):
        store(session, str(number))
    user_id = onboarded(service)
    service.confirm_delivered(
        user_id, [p.recommendation for p in service.prepare_batch(user_id, NOW)[:3]], CHANNEL, NOW
    )

    page = service.feed_page(user_id, 0, NOW, entering=False)

    assert page.total == DAILY_LIMIT
    assert [shown(service, user_id, i).card is not None for i in range(3, 7)] == [
        True,
        True,
        False,
        False,
    ]


def test_yesterdays_cards_are_not_in_todays_carousel(
    service: SeaWorkService, session: Session
) -> None:
    for number in range(3):
        store(session, str(number))
    user_id = onboarded(service)
    yesterday = shown(service, user_id, 0)
    tomorrow = NOW + datetime.timedelta(days=1)

    page = service.feed_page(user_id, 0, tomorrow, entering=False)

    assert page.card is not None
    assert yesterday.card is not None
    assert page.card.opportunity_id != yesterday.card.opportunity_id
    assert page.total == 2


def test_a_delivered_card_is_scored_by_the_profile_as_it_is_now(
    service: SeaWorkService, session: Session
) -> None:
    store(session, "1", profession="galley")
    user_id = onboarded(service)
    profiles = ProfileRepository(session, VOCABULARY)

    def say_professions(*keys: str) -> None:
        profiles.set_fact(
            user_id,
            "professions",
            Fact(value=list(keys), source=ProfileFactSource.STATED, confidence=1.0),
        )

    say_professions("galley")
    before = shown(service, user_id, 0)
    say_professions("master")
    after = service.feed_page(user_id, 0, NOW, entering=False)

    assert before.card is not None
    assert after.card is not None
    assert after.fresh is None
    assert [(r.factor, r.outcome) for r in before.card.reasons if r.factor == "profession"] == [
        ("profession", FactorOutcome.MATCH)
    ]
    assert [(r.factor, r.outcome) for r in after.card.reasons if r.factor == "profession"] == [
        ("profession", FactorOutcome.MISMATCH)
    ]


def test_a_delivered_group_keeps_the_members_it_was_delivered_with(
    service: SeaWorkService, session: Session
) -> None:
    store(session, "1", employer="Acme", title="Chef")
    store(session, "2", employer="Acme", title="Chef")
    user_id = onboarded(service)
    shown(service, user_id, 0)

    again = service.feed_page(user_id, 0, NOW, entering=False)

    assert again.card is not None
    assert again.card.similar_count == 1


def test_the_reaction_state_follows_the_journal(service: SeaWorkService, session: Session) -> None:
    store(session, "1")
    user_id = onboarded(service)
    card = shown(service, user_id, 0).card
    assert card is not None

    def state() -> ReactionState:
        return service.feed_page(user_id, 0, NOW, entering=False).reaction

    assert state() is ReactionState.NONE
    service.react(user_id, card.opportunity_id, BehaviourAction.SAVED, NOW)
    assert state() is ReactionState.SAVED
    service.react(user_id, card.opportunity_id, BehaviourAction.DISMISSED, NOW)
    assert state() is ReactionState.DISMISSED
    service.react(user_id, card.opportunity_id, BehaviourAction.REOPENED, NOW)
    assert state() is ReactionState.NONE


def test_a_dismissed_delivered_card_stays_in_todays_carousel(
    service: SeaWorkService, session: Session
) -> None:
    for number in range(2):
        store(session, str(number))
    user_id = onboarded(service)
    card = shown(service, user_id, 0).card
    assert card is not None
    service.react(user_id, card.opportunity_id, BehaviourAction.DISMISSED, NOW)

    page = service.feed_page(user_id, 0, NOW, entering=False)

    assert page.card == card
    assert page.reaction is ReactionState.DISMISSED
    assert page.total == 2


def test_a_delivered_vacancy_that_was_closed_drops_out_of_the_carousel(
    service: SeaWorkService, session: Session
) -> None:
    for number in range(3):
        store(session, str(number))
    user_id = onboarded(service)
    gone = shown(service, user_id, 0).card
    assert gone is not None
    shown(service, user_id, 1)
    session.execute(
        update(OpportunityRecord)
        .where(OpportunityRecord.id == gone.opportunity_id)
        .values(status=OpportunityStatus.CLOSED.value)
    )
    session.commit()

    page = service.feed_page(user_id, 0, NOW, entering=False)

    assert page.card is not None
    assert page.card.opportunity_id != gone.opportunity_id
    assert page.total == 2


def test_confirming_the_same_recommendation_twice_delivers_it_once(
    service: SeaWorkService, session: Session
) -> None:
    store(session, "1")
    user_id = onboarded(service)
    page = service.feed_page(user_id, 0, NOW, entering=False)
    assert page.fresh is not None

    service.confirm_delivered(user_id, [page.fresh], CHANNEL, NOW)
    service.confirm_delivered(user_id, [page.fresh], CHANNEL, NOW)

    assert count(session, DeliveryRecord) == 1


def test_notifications_switch_and_report_their_state(service: SeaWorkService) -> None:
    user_id = onboarded(service)
    assert service.notifications_enabled(user_id)

    assert service.toggle_notifications(user_id, NOW) is False
    assert not service.notifications_enabled(user_id)
    assert service.digest_recipients(CHANNEL) == []

    assert service.toggle_notifications(user_id, NOW) is True
    assert service.notifications_enabled(user_id)


def test_an_account_has_no_digest_notice_yet(service: SeaWorkService, session: Session) -> None:
    onboarded(service)

    account = session.execute(select(ChannelAccountRecord)).scalar_one()

    assert account.digest_notified_at is None


def test_a_refused_card_is_never_offered_again_but_takes_no_place_and_no_limit(
    service: SeaWorkService, session: Session
) -> None:
    for number in range(DAILY_LIMIT + 1):
        store(session, str(number))
    user_id = onboarded(service)
    refused = service.feed_page(user_id, 0, NOW, entering=False)
    assert refused.fresh is not None
    service.confirm_delivered(user_id, [refused.fresh], CHANNEL, NOW, rejected=True)

    pages = [shown(service, user_id, index) for index in range(DAILY_LIMIT + 1)]

    ids = [p.card.opportunity_id for p in pages if p.card is not None]
    assert refused.card is not None
    assert refused.card.opportunity_id not in ids
    assert len(ids) == DAILY_LIMIT
    assert pages[0].total == DAILY_LIMIT
    assert count(session, DeliveryRecord) == DAILY_LIMIT + 1
    assert service.prepare_batch(user_id, NOW) == []


def test_concurrent_confirmations_of_one_card_write_it_once(
    service: SeaWorkService, session: Session
) -> None:
    store(session, "1")
    user_id = onboarded(service)
    page = service.feed_page(user_id, 0, NOW, entering=False)
    assert page.fresh is not None
    recommendation = page.fresh
    workers = 8
    barrier = threading.Barrier(workers, timeout=10)
    failures: list[BaseException] = []

    def confirm() -> None:
        barrier.wait()
        try:
            service.confirm_delivered(user_id, [recommendation], CHANNEL, NOW)
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=confirm) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures
    assert count(session, DeliveryRecord) == 1


def test_only_opening_the_carousel_may_ask_for_the_hiring_country(
    service: SeaWorkService, session: Session
) -> None:
    for number in range(3):
        store(session, str(number), countries=["GB"])
    user_id = onboarded(service)
    shown(service, user_id, 0)
    tomorrow = NOW + datetime.timedelta(days=1)

    paging = service.feed_page(user_id, 0, tomorrow, entering=False)
    opening = service.feed_page(user_id, 0, tomorrow, entering=True)

    assert not paging.ask_hiring_country
    assert paging.card is not None
    assert opening.ask_hiring_country
    assert (opening.card, opening.fresh) == (None, None)
    service.mark_hiring_country_asked(user_id, tomorrow)
    assert not service.feed_page(user_id, 0, tomorrow, entering=True).ask_hiring_country


def test_a_person_who_has_not_answered_the_stage_still_gets_a_page(
    service: SeaWorkService, session: Session
) -> None:
    store(session, "1")
    result = service.start(CHANNEL, "100")
    assert not result.stage_known

    page = service.feed_page(result.user_id, 0, NOW, entering=True)

    assert page.card is not None
    assert page.fresh is not None
    assert not page.ask_hiring_country
