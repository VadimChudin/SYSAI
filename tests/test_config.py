hoplite/render-database-startup
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
=======
import os
import subprocess
import sys

import pytest


def config_value(name, tmp_path, **variables):
    env = dict(os.environ, DATA_DIR=str(tmp_path))
    for key in ("DATABASE_URL", "PUBLIC_URL", "RENDER_EXTERNAL_URL", "RAILWAY_PUBLIC_DOMAIN"):
        env.pop(key, None)
    env.update(variables)
    return subprocess.check_output(
        [sys.executable, "-c", f"from app import config; print(config.{name})"], env=env, text=True,
    ).strip()


@pytest.mark.parametrize("value", [None, "", "  "])
def test_blank_database_url_uses_local_sqlite(tmp_path, value):
    variables = {} if value is None else {"DATABASE_URL": value}
    assert config_value("DATABASE_URL", tmp_path, **variables) == f"sqlite:///{tmp_path}/sysai.db"


@pytest.mark.parametrize("prefix", ["postgres://", "postgresql://", "postgresql+psycopg://"])
def test_postgres_url_uses_installed_driver(tmp_path, prefix):
    suffix = "user:example@localhost/test?sslmode=require"
    assert config_value("DATABASE_URL", tmp_path, DATABASE_URL=prefix + suffix) == "postgresql+psycopg://" + suffix


def test_railway_domain_is_used_for_webhook(tmp_path):
    assert config_value("PUBLIC_URL", tmp_path, RAILWAY_PUBLIC_DOMAIN="example.up.railway.app") == "https://example.up.railway.app"


def test_explicit_public_url_overrides_provider_domains(tmp_path):
    assert config_value("PUBLIC_URL", tmp_path, PUBLIC_URL="https://example.org/",
                        RAILWAY_PUBLIC_DOMAIN="example.up.railway.app",
main
