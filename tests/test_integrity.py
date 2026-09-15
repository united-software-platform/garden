"""Проверки целостности и сверка схемы с моделью на настоящем PostgreSQL."""

import tempfile
from pathlib import Path
from typing import Any

import pytest
from garden_model.checks import checks_for, run_checks
from garden_model.descriptor import compile_model
from garden_model.gen import postgres
from garden_model.model import load_model
from garden_model.verify import verify_schema

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")


@pytest.fixture(scope="module")
def server():
    directory = Path(tempfile.mkdtemp()) / "pgdata"
    instance = pgserver.get_server(str(directory))
    yield instance
    instance.cleanup()


@pytest.fixture
def db(server, model_root):
    """Пустая база со схемой текущей модели и подключением psycopg."""
    server.psql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    descriptor = compile_model(load_model(model_root))
    for statement in postgres.baseline_statements(descriptor):
        server.psql(statement)
    connection = psycopg.connect(server.get_uri(), autocommit=True)
    connection.execute(
        "INSERT INTO model_state (version, hash) VALUES (%s, %s)",
        (descriptor["version"], descriptor["hash"]),
    )
    yield connection, descriptor
    connection.close()


def _fetch(connection):
    def run(sql: str) -> list[tuple[Any, ...]]:
        rows: list[tuple[Any, ...]] = connection.execute(sql).fetchall()
        return rows

    return run


def _node(connection, kind: str, num: int) -> int:
    row = connection.execute(
        "INSERT INTO node (kind, code_num) VALUES (%s, %s) RETURNING id", (kind, num)
    ).fetchone()
    connection.execute(
        "INSERT INTO node_revision (node_id, rev, mm_version, author) VALUES (%s, 1, 2, 'тест')",
        (row[0],),
    )
    return int(row[0])


# --- проверки целостности -------------------------------------------------


def test_запросы_проверок_выполняются_на_пустой_базе(db):
    connection, descriptor = db
    assert run_checks(checks_for(descriptor), _fetch(connection)) == []


def test_узел_без_обязательной_связи_попадает_в_отчёт(db):
    connection, descriptor = db
    _node(connection, "FR", 1)

    violations = run_checks(checks_for(descriptor), _fetch(connection))
    assert [v.check for v in violations] == ["realizes.from_cardinality.min"]
    assert "минимум 1" in violations[0].subject


def test_обязательная_связь_снимает_нарушение(db):
    connection, descriptor = db
    br = _node(connection, "BR", 1)
    fr = _node(connection, "FR", 1)
    connection.execute(
        "INSERT INTO link (rel, src_id, src_kind, dst_id, dst_kind, conf_rev)"
        " VALUES ('realizes', %s, 'FR', %s, 'BR', 1)",
        (fr, br),
    )

    assert run_checks(checks_for(descriptor), _fetch(connection)) == []


def test_связь_между_недопустимыми_типами_отвергается_схемой(db):
    connection, _ = db
    br = _node(connection, "BR", 1)
    other = _node(connection, "BR", 2)

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        connection.execute(
            "INSERT INTO link (rel, src_id, src_kind, dst_id, dst_kind, conf_rev)"
            " VALUES ('realizes', %s, 'BR', %s, 'BR', 1)",
            (br, other),
        )


def test_устаревшая_ревизия_цели_делает_связь_подозрительной(db):
    connection, descriptor = db
    br = _node(connection, "BR", 1)
    fr = _node(connection, "FR", 1)
    connection.execute(
        "INSERT INTO link (rel, src_id, src_kind, dst_id, dst_kind, conf_rev)"
        " VALUES ('realizes', %s, 'FR', %s, 'BR', 1)",
        (fr, br),
    )
    connection.execute(
        "INSERT INTO node_revision (node_id, rev, mm_version, author) VALUES (%s, 2, 2, 'тест')",
        (br,),
    )

    violations = run_checks(checks_for(descriptor), _fetch(connection))
    assert [v.check for v in violations] == ["links.suspect"]


# --- сверка схемы с моделью ----------------------------------------------


def test_схема_соответствует_модели(db):
    connection, descriptor = db
    assert verify_schema(descriptor, _fetch(connection)) == []


def test_правка_схемы_руками_обнаруживается(db):
    connection, descriptor = db
    connection.execute("ALTER TABLE br_revision ADD COLUMN sneaky text")

    drifts = verify_schema(descriptor, _fetch(connection))
    assert [str(d) for d in drifts] == [
        "br_revision.sneaky: колонка есть в хранилище, но модель её не описывает"
    ]


def test_отставание_хранилища_обнаруживается(db):
    connection, descriptor = db
    connection.execute("ALTER TABLE br_revision DROP COLUMN origin")

    drifts = verify_schema(descriptor, _fetch(connection))
    assert any("origin" in str(d) and "отсутствует" in str(d) for d in drifts)


def test_непримененные_миграции_видны_по_отпечатку(db):
    connection, descriptor = db
    connection.execute("UPDATE model_state SET hash = 'sha256:чужой', version = '1.1.0'")

    drifts = verify_schema(descriptor, _fetch(connection))
    assert any("применены не все миграции" in str(d) for d in drifts)
