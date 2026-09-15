"""Сверка фактической схемы хранилища с дескриптором.

Сверка однонаправленная: модель — истина, хранилище обязано ей соответствовать.
Расхождения сообщаются поимённо, чтобы было видно, что ожидали и что нашли.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

Fetch = Callable[[str], list[tuple]]

COLUMNS_SQL = (
    "SELECT table_name, column_name FROM information_schema.columns "
    "WHERE table_schema = 'public' ORDER BY table_name, column_name;"
)
STATE_SQL = "SELECT version, hash FROM model_state;"

#: Служебные колонки таблиц ревизий, которых нет в модели.
SERVICE_COLUMNS = frozenset({"node_id", "rev", "kind", "mm_version"})


@dataclass(frozen=True)
class Drift:
    """Одно расхождение схемы и модели."""

    where: str
    detail: str

    def __str__(self) -> str:
        return f"{self.where}: {self.detail}"


def expected_columns(descriptor: dict[str, Any]) -> dict[str, set[str]]:
    """Какие колонки таблиц ревизий описаны моделью."""
    return {
        entry["table"]: {field["column"] for field in entry["fields"]} | set(SERVICE_COLUMNS)
        for entry in descriptor["types"]
    }


def verify_schema(descriptor: dict[str, Any], fetch: Fetch) -> list[Drift]:
    """Сравнить схему с дескриптором и вернуть перечень расхождений."""
    drifts: list[Drift] = []
    actual: dict[str, set[str]] = {}
    for table, column in fetch(COLUMNS_SQL):
        actual.setdefault(table, set()).add(column)

    for table, columns in expected_columns(descriptor).items():
        if table not in actual:
            drifts.append(Drift(table, "таблица описана моделью, но в хранилище отсутствует"))
            continue
        for column in sorted(columns - actual[table]):
            drifts.append(Drift(f"{table}.{column}", "колонка описана моделью, но в хранилище отсутствует"))
        for column in sorted(actual[table] - columns):
            drifts.append(Drift(f"{table}.{column}", "колонка есть в хранилище, но модель её не описывает"))

    drifts += _verify_state(descriptor, fetch)
    return drifts


def _verify_state(descriptor: dict[str, Any], fetch: Fetch) -> list[Drift]:
    rows = fetch(STATE_SQL)
    if not rows:
        return [Drift("model_state", "отпечаток модели отсутствует: миграции не применялись")]
    version, model_hash = rows[0]
    if model_hash != descriptor["hash"]:
        return [
            Drift(
                "model_state",
                f"хранилище собрано по версии {version}, модель — {descriptor['version']}: "
                "применены не все миграции",
            )
        ]
    return []
