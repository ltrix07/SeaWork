import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from seawork.config import Settings
from seawork.storage.models import Base

# The repository speaks PostgreSQL on purpose - INSERT ... ON CONFLICT is how both
# idempotency guarantees are expressed - so these tests need a real server rather
# than SQLite. CI provides one; locally `docker compose up -d` does.
DATABASE_URL = os.environ.get(
    "SEAWORK_TEST_DATABASE_URL",
    os.environ.get(
        "SEAWORK_DATABASE_URL", "postgresql+psycopg://seawork:seawork@localhost:5432/seawork"
    ),
)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # The developer's .env holds a live bot token and a paid LLM key: a test that
    # builds Settings() must see the defaults, not whatever this machine has.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in [key for key in os.environ if key.startswith("SEAWORK_")]:
        monkeypatch.delenv(name)


@pytest.fixture
def reference_dir() -> Path:
    return Path("data/reference")


@pytest.fixture
def pinpoint_posting() -> dict[str, object]:
    return {
        "id": "43999",
        "url": "https://hollandamericagroup.pinpointhq.com/postings/43999",
        "title": "SBN - Chef De Partie - CSSI",
        "description": "<div>Directs and supervises preparation of food for guests and crew.</div>",
        "key_responsibilities_header": "Responsibilities",
        "key_responsibilities": "<ul><li>Supervises the assigned galley station.</li></ul>",
        "skills_knowledge_expertise_header": "Requirements",
        "skills_knowledge_expertise": (
            "<p>Minimum 3 years experience. STCW Basic Safety Training required.</p>"
        ),
        "benefits_header": "Travel Requirements",
        "benefits": "<p>Passport and C1/D visa required.</p>",
        "employment_type": "fixed_term_contract",
        "compensation": None,
        "location": {"name": "India - CSSI", "city": "Kurla West, Mumbai"},
        "job": {
            "division": {"name": "Hotel"},
            "department": {"name": "Galley"},
            "structure_custom_group_one": {"name": "Seabourn"},
        },
    }


@pytest.fixture
def test_schema() -> str:
    raise NotImplementedError("a module using `session` must override `test_schema`")


@pytest.fixture
def session(test_schema: str) -> Iterator[Session]:
    # Everything happens inside a schema the requesting module owns (its `test_schema` fixture).
    # The default URL points at a port that, on a developer machine, may well be
    # someone else's PostgreSQL: creating and dropping tables in `public` there would
    # be destructive in a way a test suite must never be. Dropping a schema we created
    # ourselves cannot reach anyone's data.
    schema = test_schema
    engine = create_engine(DATABASE_URL, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as connection:
            connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
    except OperationalError as error:  # pragma: no cover - depends on the environment
        engine.dispose()
        pytest.skip(f"PostgreSQL is unavailable at {DATABASE_URL}: {error}")
    # Recreated per test: these assertions count rows, and a table left over from a
    # neighbour would make them pass or fail for the wrong reason.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as opened:
        yield opened
    with engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
    engine.dispose()
