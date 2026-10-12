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
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from seawork.app import DAILY_LIMIT, SeaWorkService
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
from seawork.domain.profile import BehaviourAction, CareerStage, ProfileFactSource
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
    return SeaWorkService(sessions, VOCABULARY)


def store(
    session: Session,
    external_id: str,
    employer: str | None = None,
    profession: str = "chef",
    countries: list[str] | None = None,
    status: OpportunityStatus = OpportunityStatus.ACTIVE,
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
            title=f"Vacancy {external_id}",
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
    service.answer_stage(user_id, CareerStage.ENTRY)

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

    assert service.hiring_country_options(10) == ["PH", "IN", "ID"]
    assert service.hiring_country_options(2) == ["PH", "IN"]


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
    first = store(session, "1", employer="Acme")
    second = store(session, "2", employer="Acme")
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
    members = {store(session, str(n), employer="Acme") for n in range(3)}
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
