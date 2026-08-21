"""Test fixtures.

Each test runs inside a transaction that is rolled back afterwards, so the suite
never leaves rows behind and tests cannot see each other's data.
"""

import os

# Must precede any app import: Settings is cached, and bcrypt at the production
# cost factor dominates the suite's runtime (every fixture registers a user).
# 4 rounds is the library minimum and keeps the hashing path itself under test.
os.environ.setdefault("BCRYPT_ROUNDS", "4")

from collections.abc import Callable, Generator  # noqa: E402
from dataclasses import dataclass

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_db
from app.main import app
from app.models import (  # noqa: F401 — registers the tables
    Expense,
    ExpenseSplit,
    Friendship,
    Group,
    GroupMember,
    Invitation,
    KhataAccount,
    KhataEntry,
    RefreshToken,
    Settlement,
    User,
)

# Swap only the trailing database name — the username can match it, so a plain
# str.replace would corrupt the credentials.
_base_uri, _, _database = settings.sqlalchemy_database_uri.rpartition("/")
TEST_DATABASE_URI = f"{_base_uri}/{_database}_test"

engine = create_engine(TEST_DATABASE_URI, pool_pre_ping=True)
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture(scope="session", autouse=True)
def _schema() -> Generator[None, None, None]:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db() -> Generator[Session, None, None]:
    """A session bound to a transaction that is rolled back when the test ends."""
    connection = engine.connect()
    transaction = connection.begin()
    session = TestingSessionLocal(bind=connection)

    # The app commits during a request; join_transaction_mode keeps those commits
    # inside the outer transaction so the rollback below still undoes everything.
    session.connection(execution_options={"join_transaction_mode": "create_savepoint"})

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def client(db: Session) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def user_payload() -> dict[str, str]:
    return {
        "email": "ada@example.com",
        "full_name": "Ada Lovelace",
        "password": "hunter2pass",
    }


@pytest.fixture
def registered(client: TestClient, user_payload: dict[str, str]) -> dict:
    response = client.post("/api/v1/auth/register", json=user_payload)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def auth_headers(registered: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {registered['access_token']}"}


# --------------------------------------------------------------------------- #
# Multi-user helpers, for friends / groups / expenses
# --------------------------------------------------------------------------- #
@dataclass
class Actor:
    """A registered user plus the headers needed to act as them."""

    id: str
    email: str
    full_name: str
    headers: dict[str, str]


@pytest.fixture
def make_user(client: TestClient) -> Callable[..., Actor]:
    counter = {"n": 0}

    def _make(name: str | None = None) -> Actor:
        counter["n"] += 1
        index = counter["n"]
        email = f"user{index}@example.com"
        full_name = name or f"User {index}"

        response = client.post(
            "/api/v1/auth/register",
            json={"email": email, "full_name": full_name, "password": "hunter2pass"},
        )
        assert response.status_code == 201, response.text
        body = response.json()

        return Actor(
            id=body["user"]["id"],
            email=email,
            full_name=full_name,
            headers={"Authorization": f"Bearer {body['access_token']}"},
        )

    return _make


@pytest.fixture
def alice(make_user: Callable[..., Actor]) -> Actor:
    return make_user("Alice Anderson")


@pytest.fixture
def bob(make_user: Callable[..., Actor]) -> Actor:
    return make_user("Bob Brown")


@pytest.fixture
def carol(make_user: Callable[..., Actor]) -> Actor:
    return make_user("Carol Clark")


@pytest.fixture
def befriend(client: TestClient) -> Callable[[Actor, Actor], None]:
    """Put two users into an accepted friendship."""

    def _befriend(first: Actor, second: Actor) -> None:
        sent = client.post(
            "/api/v1/friends/requests", json={"user_id": second.id}, headers=first.headers
        )
        assert sent.status_code == 201, sent.text
        accepted = client.post(
            f"/api/v1/friends/requests/{sent.json()['id']}/accept", headers=second.headers
        )
        assert accepted.status_code == 200, accepted.text

    return _befriend


@pytest.fixture
def make_group(client: TestClient) -> Callable[..., dict]:
    def _make(owner: Actor, *members: Actor, name: str = "Trip") -> dict:
        response = client.post(
            "/api/v1/groups",
            json={"name": name, "member_ids": [member.id for member in members]},
            headers=owner.headers,
        )
        assert response.status_code == 201, response.text
        return response.json()

    return _make


@pytest.fixture(autouse=True)
def _reset_tables(db: Session) -> Generator[None, None, None]:
    yield
    db.execute(
        text(
            "TRUNCATE reminders, notes, loan_payments, loans, khata_entries, khata_accounts, "
            "settlements, expense_splits, "
            "expenses, group_members, groups, friendships, invitations, "
            "refresh_tokens, users RESTART IDENTITY CASCADE"
        )
    )
    db.commit()
