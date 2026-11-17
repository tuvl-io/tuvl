"""What a run records and a caller may see of an engine error: the kind of failure, not other people's data."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from tuvl.core.datasources import postgres
from tuvl.runtime.runner import exception_message


def test_a_database_error_keeps_the_constraint_and_drops_the_values() -> None:
    exc = IntegrityError(
        "INSERT INTO people (email) VALUES (%s)",
        {"email": "ann@example.com"},
        Exception(
            'duplicate key value violates unique constraint "people_email_key"\n'
            "DETAIL:  Key (email)=(ann@example.com) already exists."
        ),
    )
    message = exception_message(exc)
    assert "people_email_key" in message
    assert "ann@example.com" not in message and "INSERT" not in message


def test_another_error_reads_as_before() -> None:
    assert exception_message(ValueError("bad value")) == "ValueError: bad value"


def test_engines_do_not_put_statement_parameters_in_errors(monkeypatch) -> None:
    import tuvl.core.datasources.loader as loader

    for registry in ("_engines_rw", "_engines_ro", "_sessions_rw", "_sessions_ro"):
        monkeypatch.setattr(postgres, registry, {})
    monkeypatch.setattr(
        postgres,
        "DATASOURCE_REGISTRY",
        {
            "main": {
                "spec": {
                    "type": "postgres",
                    "connection": {
                        "host": "localhost",
                        "port": 5432,
                        "database": "d",
                        "username": "u",
                        "password": "p",
                    },
                }
            }
        },
    )
    monkeypatch.setattr(loader, "PRIMARY_DS", "main")
    postgres._build_engines()
    assert postgres._engines_rw["main"].sync_engine.hide_parameters is True
    assert postgres._engines_ro["main"].sync_engine.hide_parameters is True
