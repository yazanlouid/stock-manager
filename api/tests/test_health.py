from collections.abc import Iterator

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.main import app


def test_health_reports_database_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_tests_run_against_the_test_database(db_session: Session) -> None:
    # Guard: tests must never touch the development database.
    assert db_session.scalar(text("SELECT current_database()")) == "stock_test"


def test_health_returns_503_when_database_is_unreachable() -> None:
    # Same credentials, but a port where nothing is listening.
    url = make_url(get_settings().database_url).set(host="127.0.0.1", port=1)
    unreachable = create_engine(url, connect_args={"connect_timeout": 2})

    def unreachable_db() -> Iterator[Session]:
        with Session(unreachable) as session:
            yield session

    app.dependency_overrides[get_db] = unreachable_db
    try:
        with TestClient(app) as test_client:
            response = test_client.get("/health")
    finally:
        app.dependency_overrides.clear()
        unreachable.dispose()

    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
