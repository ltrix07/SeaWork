"""Profile module: absence versus emptiness, provenance, and what deletion removes.

Follows test_storage.py: its own schema, skipped when no PostgreSQL is reachable.
The domain tests at the top need no database.
"""

import datetime
import hashlib
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import HttpUrl, ValidationError
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from seawork.domain.enums import OpportunityStatus, OpportunityType, Provenance, SourceTrust
from seawork.domain.inferred import Inferred
from seawork.domain.models import EnrichedOpportunity, NormalizedOpportunity
from seawork.domain.profile import (
    FACT_FIELDS,
    BehaviourAction,
    BehaviourEvent,
    CareerGoal,
    CareerStage,
    Fact,
    ProfileFactSource,
    UserProfile,
)
from seawork.ingestion.base import RawItem
from seawork.storage.models import Base, BehaviourEventRecord, UserProfileRecord
from seawork.storage.profile_repository import ProfileRepository, StatedFactProtectedError
from seawork.storage.profile_vocabulary import ProfileVocabulary, UnknownReferenceKeyError
from seawork.storage.repository import Repository

DATABASE_URL = os.environ.get(
    "SEAWORK_TEST_DATABASE_URL",
    os.environ.get(
        "SEAWORK_DATABASE_URL", "postgresql+psycopg://seawork:seawork@localhost:5432/seawork"
    ),
)

# Own schema, for the reason given in test_storage.py: never touch `public` on a
# server that may belong to someone else.
TEST_SCHEMA = "seawork_profile_test"

# Read once, as the repository is meant to be constructed in production.
VOCABULARY = ProfileVocabulary.from_directory(Path("data/reference"))

NOW = datetime.datetime(2026, 9, 29, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(DATABASE_URL, connect_args={"options": f"-csearch_path={TEST_SCHEMA}"})
    try:
        with engine.begin() as connection:
            connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {TEST_SCHEMA}"))
    except OperationalError as error:  # pragma: no cover - depends on the environment
        engine.dispose()
        pytest.skip(f"PostgreSQL is unavailable at {DATABASE_URL}: {error}")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as opened:
        yield opened
    with engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
    engine.dispose()


def stated[T](value: T) -> Fact[T]:
    return Fact(value=value, source=ProfileFactSource.STATED, confidence=1.0, stated_at=NOW)


def observed[T](value: T, confidence: float = 0.4) -> Fact[T]:
    return Fact(value=value, source=ProfileFactSource.OBSERVED, confidence=confidence)


def event(user_id: uuid.UUID, opportunity_id: int, action: BehaviourAction) -> BehaviourEvent:
    return BehaviourEvent(
        user_id=user_id, opportunity_id=opportunity_id, action=action, occurred_at=NOW
    )


def store_opportunity(
    session: Session, external_id: str, direction: str | None, profession: str | None
) -> int:
    """Insert a vacancy through the real repository and return its primary key."""
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
    opportunity = EnrichedOpportunity(
        normalized=NormalizedOpportunity(
            source_id="padi",
            external_id=external_id,
            url=HttpUrl(f"https://example.com/job/{external_id}"),
            title="Vacancy",
        ),
        type=OpportunityType.JOB,
        type_confidence=0.9,
        direction=None
        if direction is None
        else Inferred(value=direction, provenance=Provenance.RULE, confidence=0.8),
        profession=None
        if profession is None
        else Inferred(value=profession, provenance=Provenance.RULE, confidence=0.8),
        quality_score=1.0,
        quality_flags=[],
    )
    Repository(session).upsert_opportunity(
        raw.id, opportunity, SourceTrust.PRIMARY, OpportunityStatus.ACTIVE
    )
    from seawork.storage.models import OpportunityRecord

    return session.scalars(
        select(OpportunityRecord.id).where(OpportunityRecord.external_id == external_id)
    ).one()


# --- domain: no database needed -------------------------------------------------


def test_stated_fact_cannot_claim_less_than_certainty() -> None:
    """A person's own answer is not an inference; a lower number would invite overwriting."""
    with pytest.raises(ValidationError):
        Fact(value="ES", source=ProfileFactSource.STATED, confidence=0.7)


def test_observed_fact_may_be_uncertain() -> None:
    assert observed("ES", 0.3).confidence == 0.3


def test_confidence_is_bounded() -> None:
    with pytest.raises(ValidationError):
        observed("ES", 1.2)


def test_every_fact_field_of_the_profile_is_covered() -> None:
    """The storage guard is derived from the model, so a new fact needs no second edit."""
    assert {"career_stage", "hiring_country", "languages"} <= FACT_FIELDS
    assert not FACT_FIELDS & {"user_id", "created_at", "updated_at"}


def test_negative_dwell_time_is_rejected() -> None:
    with pytest.raises(ValidationError):
        BehaviourEvent(
            user_id=uuid.uuid4(),
            opportunity_id=1,
            action=BehaviourAction.VIEWED,
            dwell_seconds=-1,
            occurred_at=NOW,
        )


def test_career_goal_needs_some_content() -> None:
    """An empty goal would be read by matching as a constraint, not as "no goal"."""
    with pytest.raises(ValidationError):
        CareerGoal()
    with pytest.raises(ValidationError):
        CareerGoal(country="")
    assert CareerGoal(country="NO").country == "NO"


# --- storage --------------------------------------------------------------------


def test_new_profile_is_empty_and_valid(session: Session) -> None:
    """Contract 3.1: no answer has been given, and that is a working state."""
    profile = ProfileRepository(session, VOCABULARY).get_or_create_profile()
    assert all(getattr(profile, name) is None for name in FACT_FIELDS)


def test_get_or_create_is_idempotent(session: Session) -> None:
    """A second first-message from the same person must find the profile, not fail."""
    repository = ProfileRepository(session, VOCABULARY)
    user_id = uuid.uuid4()
    first = repository.get_or_create_profile(user_id)
    repository.set_fact(user_id, "career_stage", stated(CareerStage.ENTRY))
    second = repository.get_or_create_profile(user_id)
    assert first.user_id == second.user_id
    assert second.career_stage is not None
    assert session.execute(select(func.count()).select_from(UserProfileRecord)).scalar_one() == 1


def test_get_profile_of_stranger_is_none(session: Session) -> None:
    assert ProfileRepository(session, VOCABULARY).get_profile(uuid.uuid4()) is None


def test_unknown_is_distinguishable_from_empty(session: Session) -> None:
    """Contract 3.2: `None` is "unknown", `[]` is "known to be none", and both survive a reload."""
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, "certificates", stated([]))
    session.expire_all()

    profile = repository.get_profile(user_id)
    assert profile is not None
    assert profile.certificates is not None
    assert profile.certificates.value == []
    assert profile.languages is None


def test_clearing_a_fact_returns_it_to_unknown_not_to_empty(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, "languages", stated(["en"]))
    cleared = repository.clear_fact(user_id, "languages", ProfileFactSource.STATED)
    assert cleared.languages is None


def test_setting_one_fact_leaves_the_others(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, "career_stage", stated(CareerStage.UNDECIDED))
    profile = repository.set_fact(user_id, "hiring_country", stated(["PH"]))
    assert profile.career_stage is not None
    assert profile.career_stage.value is CareerStage.UNDECIDED
    assert profile.hiring_country is not None
    assert profile.hiring_country.value == ["PH"]


def test_fact_keeps_provenance_through_storage(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, "directions", observed(["cruise"], 0.4))
    session.expire_all()
    profile = repository.get_profile(user_id)
    assert profile is not None and profile.directions is not None
    assert profile.directions.source is ProfileFactSource.OBSERVED
    assert profile.directions.confidence == 0.4
    assert profile.directions.stated_at.tzinfo is not None


def test_observation_cannot_overwrite_a_stated_answer(session: Session) -> None:
    """Contract 3.3: a guess may be withdrawn silently, an answer may not."""
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, "directions", stated(["diving"]))
    with pytest.raises(StatedFactProtectedError):
        repository.set_fact(user_id, "directions", observed(["offshore"]))
    profile = repository.get_profile(user_id)
    assert profile is not None and profile.directions is not None
    assert profile.directions.value == ["diving"]


def test_stated_answer_may_replace_an_observation_and_a_correction(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, "directions", observed(["offshore"]))
    repository.set_fact(user_id, "directions", stated(["diving"]))
    profile = repository.set_fact(user_id, "directions", stated(["cruise"]))
    assert profile.directions is not None
    assert profile.directions.value == ["cruise"]


def test_observation_may_replace_an_observation(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, "directions", observed(["offshore"], 0.3))
    profile = repository.set_fact(user_id, "directions", observed(["offshore", "cruise"], 0.5))
    assert profile.directions is not None
    assert profile.directions.confidence == 0.5


def test_unknown_fact_name_is_refused(session: Session) -> None:
    """A typo stored in a JSON document would otherwise be silently ignored on read."""
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    with pytest.raises(ValueError, match="Unknown profile fact"):
        repository.set_fact(user_id, "profession", stated(["captain"]))


def test_value_of_the_wrong_shape_is_refused_before_it_is_stored(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    with pytest.raises(ValidationError):
        repository.set_fact(user_id, "languages", stated("en"))
    profile = repository.get_profile(user_id)
    assert profile is not None and profile.languages is None


def test_setting_a_fact_of_a_missing_profile_fails_loudly(session: Session) -> None:
    with pytest.raises(LookupError):
        ProfileRepository(session, VOCABULARY).set_fact(uuid.uuid4(), "citizenship", stated("PL"))


def test_setting_a_fact_moves_updated_at_but_not_created_at(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    created = repository.get_or_create_profile()
    later = repository.set_fact(created.user_id, "citizenship", stated("PL"))
    assert later.created_at == created.created_at
    assert later.updated_at >= created.updated_at


# --- journal --------------------------------------------------------------------


def test_events_round_trip_in_order(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.record_event(event(user_id, 1, BehaviourAction.VIEWED))
    repository.record_event(
        BehaviourEvent(
            user_id=user_id,
            opportunity_id=1,
            action=BehaviourAction.SAVED,
            dwell_seconds=42,
            occurred_at=NOW + datetime.timedelta(seconds=5),
        )
    )
    events = repository.list_events(user_id)
    assert [e.action for e in events] == [BehaviourAction.VIEWED, BehaviourAction.SAVED]
    assert events[0].dwell_seconds is None
    assert events[1].dwell_seconds == 42


def test_repeated_identical_events_are_all_kept(session: Session) -> None:
    """Three views are three rows: counting them is exactly what the journal is for."""
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    for _ in range(3):
        repository.record_event(event(user_id, 7, BehaviourAction.VIEWED))
    assert len(repository.list_events(user_id)) == 3


def test_event_for_a_person_without_a_profile_is_rejected(session: Session) -> None:
    with pytest.raises(IntegrityError):
        ProfileRepository(session, VOCABULARY).record_event(
            event(uuid.uuid4(), 1, BehaviourAction.VIEWED)
        )


def test_events_are_scoped_to_their_owner(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    first = repository.get_or_create_profile().user_id
    second = repository.get_or_create_profile().user_id
    repository.record_event(event(first, 1, BehaviourAction.VIEWED))
    assert repository.list_events(second) == []


def test_interest_counts_are_plain_counters(session: Session) -> None:
    """Counts per action, direction and profession; no weights, no thresholds."""
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    cruise = store_opportunity(session, "a", "cruise", "chef")
    diving = store_opportunity(session, "b", "diving", "instructor")
    unlabelled = store_opportunity(session, "c", None, None)

    for _ in range(2):
        repository.record_event(event(user_id, cruise, BehaviourAction.VIEWED))
    repository.record_event(event(user_id, cruise, BehaviourAction.SAVED))
    repository.record_event(event(user_id, diving, BehaviourAction.DISMISSED))
    repository.record_event(event(user_id, unlabelled, BehaviourAction.VIEWED))
    repository.record_event(event(user_id, 99999, BehaviourAction.VIEWED))  # no such vacancy

    counts = repository.interest_counts(user_id)
    assert counts.by_action == {
        BehaviourAction.VIEWED: 4,
        BehaviourAction.SAVED: 1,
        BehaviourAction.DISMISSED: 1,
    }
    assert counts.by_direction["cruise"] == {BehaviourAction.VIEWED: 2, BehaviourAction.SAVED: 1}
    assert counts.by_direction["diving"] == {BehaviourAction.DISMISSED: 1}
    assert counts.by_direction[None] == {BehaviourAction.VIEWED: 2}
    assert counts.by_profession["chef"] == {BehaviourAction.VIEWED: 2, BehaviourAction.SAVED: 1}


def test_interest_counts_of_a_silent_user_are_empty(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    counts = repository.interest_counts(user_id)
    assert counts.by_action == {} and counts.by_direction == {}


# --- deletion and export --------------------------------------------------------


def test_deleting_a_user_removes_profile_and_journal(session: Session) -> None:
    """Contract 3.6: the journal is the more personal of the two and must go too."""
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, "citizenship", stated("PL"))
    repository.record_event(event(user_id, 1, BehaviourAction.VIEWED))

    assert repository.delete_user(user_id) is True
    assert repository.get_profile(user_id) is None
    assert session.execute(select(func.count()).select_from(BehaviourEventRecord)).scalar_one() == 0


def test_deleting_one_user_leaves_another(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    gone = repository.get_or_create_profile().user_id
    kept = repository.get_or_create_profile().user_id
    repository.record_event(event(gone, 1, BehaviourAction.VIEWED))
    repository.record_event(event(kept, 1, BehaviourAction.VIEWED))

    repository.delete_user(gone)
    assert len(repository.list_events(kept)) == 1
    assert repository.get_profile(kept) is not None


def test_deleting_a_stranger_reports_nobody_was_there(session: Session) -> None:
    assert ProfileRepository(session, VOCABULARY).delete_user(uuid.uuid4()) is False


def test_database_cascade_also_removes_the_journal(session: Session) -> None:
    """The guarantee must hold for a delete that bypasses the repository."""
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.record_event(event(user_id, 1, BehaviourAction.VIEWED))
    session.execute(text("DELETE FROM user_profiles"))
    session.commit()
    assert session.execute(select(func.count()).select_from(BehaviourEventRecord)).scalar_one() == 0


def test_export_contains_facts_and_journal(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, "languages", stated(["en", "es"]))
    repository.record_event(event(user_id, 3, BehaviourAction.APPLIED))

    exported = repository.export_user(user_id)
    assert exported is not None
    assert exported.profile.languages is not None
    assert exported.profile.languages.value == ["en", "es"]
    assert [e.action for e in exported.events] == [BehaviourAction.APPLIED]
    # The export must survive being written out as JSON, which is how it is delivered.
    assert (
        UserProfile.model_validate(exported.model_dump(mode="json")["profile"]) == exported.profile
    )


def test_export_of_a_stranger_is_none(session: Session) -> None:
    assert ProfileRepository(session, VOCABULARY).export_user(uuid.uuid4()) is None


# --- reference vocabulary -------------------------------------------------------


def test_unknown_reference_key_is_an_error_not_a_silent_drop(session: Session) -> None:
    """A person's answer must not vanish unnoticed the way a model's guess may."""
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    with pytest.raises(UnknownReferenceKeyError) as raised:
        repository.set_fact(user_id, "professions", stated(["master", "banana"]))
    assert raised.value.unknown == ["banana"]
    profile = repository.get_profile(user_id)
    assert profile is not None and profile.professions is None


@pytest.mark.parametrize(
    ("field", "good", "bad"),
    [
        ("professions", ["master"], ["nope"]),
        ("directions", ["cruise"], ["nope"]),
        ("certificates", ["stcw"], ["nope"]),
        ("languages", ["en"], ["nope"]),
        ("citizenship", "PL", "P1"),
        ("residence_country", "PH", "PHL"),
        ("preferred_countries", ["PH"], ["PHL"]),
        ("hiring_country", ["PH", "GB"], ["PH", "G"]),
        ("work_authorization", ["UA", "NO"], ["UA", "Norway"]),
    ],
)
def test_every_vocabulary_field_is_checked(
    session: Session, field: str, good: object, bad: object
) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, field, stated(good))
    with pytest.raises(UnknownReferenceKeyError):
        repository.set_fact(user_id, field, stated(bad))


def test_empty_list_passes_vocabulary_check(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, "professions", stated([]))


def test_career_goal_keys_are_checked(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(
        user_id, "career_goal", stated(CareerGoal(country="PL", direction="science"))
    )
    with pytest.raises(UnknownReferenceKeyError) as raised:
        repository.set_fact(user_id, "career_goal", stated(CareerGoal(direction="nope")))
    assert raised.value.field == "career_goal.direction"


def test_hiring_country_holds_several_countries(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    profile = repository.set_fact(user_id, "hiring_country", stated(["PH", "GB"]))
    assert profile.hiring_country is not None
    assert profile.hiring_country.value == ["PH", "GB"]


# --- clearing is protected like setting -----------------------------------------


def test_background_analytics_cannot_clear_a_stated_fact(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, "languages", stated(["en"]))
    for source in (ProfileFactSource.OBSERVED, ProfileFactSource.IMPORTED):
        with pytest.raises(StatedFactProtectedError):
            repository.clear_fact(user_id, "languages", source)
    profile = repository.get_profile(user_id)
    assert profile is not None and profile.languages is not None


def test_analytics_may_clear_its_own_guess(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, "languages", observed(["en"]))
    cleared = repository.clear_fact(user_id, "languages", ProfileFactSource.OBSERVED)
    assert cleared.languages is None


def test_clearing_something_never_set_is_harmless(session: Session) -> None:
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.clear_fact(user_id, "languages", ProfileFactSource.OBSERVED)


def test_country_codes_are_checked_by_shape_not_by_our_dictionary(session: Session) -> None:
    """Ukraine and Norway are absent from countries.yaml; a person from either must still fit.

    The file is a text-parsing dictionary, not a list of countries. A well-formed code
    we have never seen matches nothing and harms no one.
    """
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, "citizenship", stated("UA"))
    profile = repository.set_fact(user_id, "career_goal", stated(CareerGoal(country="NO")))
    assert profile.citizenship is not None and profile.citizenship.value == "UA"
    with pytest.raises(UnknownReferenceKeyError):
        repository.set_fact(user_id, "career_goal", stated(CareerGoal(country="norway")))


def test_country_codes_are_upper_cased_rather_than_refused(session: Session) -> None:
    """A lower-case code is canonicalised, not rejected.

    Canonicalising is not inferring: an alpha-2 code has one upper-case form, so
    "pl" -> "PL" invents nothing. Refusing it would make the boundary brittle over a
    mistake with a single unambiguous fix. Reading a bare "$" as USD, by contrast, is
    a guess, and parse_salary lowers its confidence to say so.
    """
    repository = ProfileRepository(session, VOCABULARY)
    user_id = repository.get_or_create_profile().user_id
    repository.set_fact(user_id, "citizenship", stated("ua"))
    repository.set_fact(user_id, "work_authorization", stated(["pl", "No"]))
    profile = repository.get_profile(user_id)
    assert profile is not None
    assert profile.citizenship is not None and profile.citizenship.value == "UA"
    assert profile.work_authorization is not None
    assert profile.work_authorization.value == ["PL", "NO"]
