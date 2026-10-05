import pathlib

import pytest
from sqlalchemy import create_engine

from app.config import database_url


@pytest.mark.parametrize("value", [None, "", "   \t"])
def test_missing_or_blank_database_url_uses_sqlite(tmp_path, value):
    url = database_url(value, tmp_path)
    assert url == f"sqlite:///{tmp_path / 'sysai.db'}"
    engine = create_engine(url)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT 1").scalar() == 1
    engine.dispose()


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg"])
def test_provider_postgres_url_uses_installed_driver(scheme):
    value = f"{scheme}://example:example@localhost/example?sslmode=require"
    normalized = database_url(f"  {value}  ", pathlib.Path("data"))
    assert normalized == "postgresql+psycopg://example:example@localhost/example?sslmode=require"
    engine = create_engine(normalized)
    assert engine.dialect.driver == "psycopg"
    engine.dispose()


def test_explicit_sqlite_url_is_preserved():
    assert database_url("sqlite:///:memory:", pathlib.Path("data")) == "sqlite:///:memory:"
