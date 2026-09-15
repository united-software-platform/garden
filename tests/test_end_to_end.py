"""Сквозной прогон: пять выпусков модели, накат на настоящий PostgreSQL, сверка и проверки.

Тест повторяет ручную проверку инструмента на демонстрационной модели предметной области
«торговля». Именно такой прогон нашёл три ошибки, которых не видели остальные тесты:
столкновение имён файлов, ссылку условного ограничения на колонку чужой таблицы
и расхождение хеша после пополнения занятых номеров.
"""

import tempfile
from pathlib import Path

import pytest
import yaml

from conftest import statements_of
from garden_model.checks import checks_for, run_checks
from garden_model.cli import main
from garden_model.descriptor import compile_model
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
def db(server):
    server.psql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    connection = psycopg.connect(server.get_uri(), autocommit=True)
    yield connection
    connection.close()


def _edit(root: Path, name: str, mutate):
    path = root / name
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _release(root: Path, workspace: Path, version: str | None, *extra) -> int:
    if version is not None:
        _edit(root, "_manifest.yaml", lambda d: d.update(version=version))
    return main([
        "gen",
        "--model", str(root),
        "--releases", str(workspace / "releases"),
        "--changelog", str(workspace / "changelog"),
        *extra,
    ])


def _evolve(root: Path, workspace: Path) -> None:
    """Пять выпусков: свёртка, добавление, обязательность с версии, переименование, удаление."""
    assert _release(root, workspace, None, "--baseline") == 0

    _edit(root, "types/ord.yaml", lambda d: d["fields"].append({"id": 6, "name": "discount", "type": "int"}))
    _edit(root, "types/prod.yaml", lambda d: [
        f.update(required=True, required_since=1) for f in d["fields"] if f["name"] == "active"
    ])
    assert _release(root, workspace, "1.1.0") == 0

    _edit(root, "types/ord.yaml", lambda d: [
        f.update(name="note") for f in d["fields"] if f["name"] == "comment"
    ])
    assert _release(root, workspace, "1.2.0") == 0

    _edit(root, "types/ship.yaml", lambda d: d.__setitem__(
        "fields", [f for f in d["fields"] if f["name"] != "tracking"]
    ))
    assert _release(root, workspace, "1.3.0") == 0

    _edit(root, "types/cust.yaml", lambda d: d["fields"].append({"id": 4, "name": "phone", "type": "text"}))
    assert _release(root, workspace, "1.4.0") == 0


def _apply_all(connection, changelog: Path) -> int:
    """Применить миграции в порядке, заданном master.yaml, а не сортировкой имён."""
    master = yaml.safe_load((changelog / "master.yaml").read_text(encoding="utf-8"))
    files = [entry["include"]["file"] for entry in master["databaseChangeLog"]]
    for name in files:
        for statement in statements_of(changelog / name):
            connection.execute(statement)
    return len(files)


def _fetch(connection):
    def run(sql: str) -> list[tuple]:
        return connection.execute(sql).fetchall()

    return run


def _node(connection, kind: str, num: int, table: str, **fields) -> int:
    """Завести узел вместе с первой ревизией и строкой ревизии его типа."""
    node_id = connection.execute(
        "INSERT INTO node (kind, code_num) VALUES (%s, %s) RETURNING id", (kind, num)
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO node_revision (node_id, rev, mm_version, author) VALUES (%s, 1, 4, 'тест')",
        (node_id,),
    )
    columns = ", ".join(["node_id", "rev", "mm_version", *fields])
    holders = ", ".join(["%s", "1", "4", *["%s"] * len(fields)])
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({holders})", (node_id, *fields.values())
    )
    return node_id


def _link(connection, rel: str, src: tuple[int, str], dst: tuple[int, str]) -> None:
    connection.execute(
        "INSERT INTO link (rel, src_id, src_kind, dst_id, dst_kind, conf_rev)"
        " VALUES (%s, %s, %s, %s, %s, 1)",
        (rel, src[0], src[1], dst[0], dst[1]),
    )


@pytest.fixture
def applied(db, trade_copy, tmp_path):
    """База, собранная полным прогоном всех выпущенных миграций."""
    workspace = tmp_path / "workspace"
    _evolve(trade_copy, workspace)
    count = _apply_all(db, workspace / "changelog")
    descriptor = compile_model(load_model(trade_copy))
    return db, descriptor, count


def test_вся_история_миграций_применяется(applied):
    connection, descriptor, count = applied

    # 1.0.0: свёртка и замыкающая; 1.1.0: два шага и замыкающая;
    # 1.2.0, 1.3.0, 1.4.0: по одному шагу и замыкающей.
    assert count == 11
    assert connection.execute("SELECT version, hash FROM model_state").fetchone() == (
        descriptor["version"],
        descriptor["hash"],
    )


def test_схема_после_наката_соответствует_модели(applied):
    connection, descriptor, _ = applied

    assert verify_schema(descriptor, _fetch(connection)) == []


def test_эволюция_отражена_в_схеме(applied):
    connection, _, _ = applied

    def columns(table: str) -> set[str]:
        return {
            row[0] for row in connection.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
            ).fetchall()
        }

    assert "note" in columns("ord_revision") and "comment" not in columns("ord_revision")
    assert "discount" in columns("ord_revision")
    assert "tracking" not in columns("ship_revision")
    assert "phone" in columns("cust_revision")
    assert connection.execute(
        "SELECT conname FROM pg_constraint WHERE conname = 'prod_active_since_1'"
    ).fetchone() is not None


def test_согласованные_данные_проходят_проверки(applied):
    connection, descriptor, _ = applied
    _fill(connection)

    assert run_checks(checks_for(descriptor), _fetch(connection)) == []


def test_заказ_без_покупателя_попадает_в_отчёт(applied):
    connection, descriptor, _ = applied
    _fill(connection)
    _node(connection, "ORD", 2, "ord_revision", number="A-2", status="DRAFT", placed_at="now()")

    violations = run_checks(checks_for(descriptor), _fetch(connection))
    assert [v.check for v in violations] == ["placed_by.from_cardinality.min"]


def test_два_покупателя_у_заказа_попадают_в_отчёт(applied):
    """Верхняя граница кардинальности: схема её не держит, держит проверка целостности."""
    connection, descriptor, _ = applied
    ids = _fill(connection)
    another = _node(connection, "CUST", 2, "cust_revision", full_name="Второй", email="b@x", registered_at="now()")
    _link(connection, "placed_by", (ids["ord"], "ORD"), (another, "CUST"))

    violations = run_checks(checks_for(descriptor), _fetch(connection))
    assert [v.check for v in violations] == ["placed_by.from_cardinality.max"]


def test_цикл_по_связи_замены_товара_обнаруживается(applied):
    connection, descriptor, _ = applied
    ids = _fill(connection)
    other = _node(connection, "PROD", 2, "prod_revision", title="Замена", sku="S-2", price=200, active=True)
    _link(connection, "replaces", (ids["prod"], "PROD"), (other, "PROD"))
    _link(connection, "replaces", (other, "PROD"), (ids["prod"], "PROD"))

    violations = run_checks(checks_for(descriptor), _fetch(connection))
    assert "replaces.acyclic" in [v.check for v in violations]


def _fill(connection) -> dict[str, int]:
    """Согласованный набор данных: покупатель, товар, заказ, строка заказа и их связи."""
    cust = _node(connection, "CUST", 1, "cust_revision",
                 full_name="Иванов", email="i@example.com", registered_at="now()", phone="+7")
    prod = _node(connection, "PROD", 1, "prod_revision", title="Чайник", sku="SKU-1", price=199900, active=True)
    ord_id = _node(connection, "ORD", 1, "ord_revision",
                   number="A-1", status="PAID", payment="CARD", placed_at="now()", note="срочно", discount=0)
    item = _node(connection, "ITEM", 1, "item_revision", quantity=2, price=199900)

    _link(connection, "placed_by", (ord_id, "ORD"), (cust, "CUST"))
    _link(connection, "belongs_to", (item, "ITEM"), (ord_id, "ORD"))
    _link(connection, "refers_to", (item, "ITEM"), (prod, "PROD"))
    return {"cust": cust, "prod": prod, "ord": ord_id, "item": item}
