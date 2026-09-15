"""Проверки целостности, которые невозможно выразить ограничениями схемы.

Нижние границы кардинальности, ацикличность и подтверждённые ревизии целей — это
состояние данных, а не схемы. Нарушение такой проверки не отвергает запись: оно
сообщается как расхождение, подлежащее устранению.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

Fetch = Callable[[str], list[tuple[Any, ...]]]


@dataclass(frozen=True)
class Check:
    """Одна проверка: имя, пояснение и запрос, возвращающий нарушения."""

    name: str
    subject: str
    sql: str


@dataclass(frozen=True)
class Violation:
    check: str
    subject: str
    rows: list[tuple[Any, ...]]

    def __str__(self) -> str:
        listing = ", ".join(str(row[0]) for row in self.rows[:5])
        tail = f" и ещё {len(self.rows) - 5}" if len(self.rows) > 5 else ""
        return f"{self.check}: {self.subject}; нарушители: {listing}{tail}"


def checks_for(descriptor: dict[str, Any]) -> list[Check]:
    """Собрать перечень проверок целостности из дескриптора."""
    return [
        *_cardinality_checks(descriptor),
        *_acyclicity_checks(descriptor),
        suspect_links_check(),
    ]


def _cardinality_checks(descriptor: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []
    for relation in descriptor["relations"]:
        for side, column, kinds in (
            ("from_cardinality", "src_id", relation["from"]),
            ("to_cardinality", "dst_id", relation["to"]),
        ):
            listing = ", ".join(f"'{code}'" for code in kinds)
            minimum, maximum = relation[side]["min"], relation[side]["max"]
            if minimum >= 1:
                checks.append(
                    _cardinality_check(
                        relation["name"],
                        side,
                        column,
                        listing,
                        f"узел без обязательной связи {relation['name']} (минимум {minimum})",
                        f"< {minimum}",
                        suffix="min",
                    )
                )
            if maximum is not None:
                checks.append(
                    _cardinality_check(
                        relation["name"],
                        side,
                        column,
                        listing,
                        f"узел со связями {relation['name']} сверх предела (максимум {maximum})",
                        f"> {maximum}",
                        suffix="max",
                    )
                )
    return checks


def _cardinality_check(
    relation: str, side: str, column: str, kinds: str, subject: str, condition: str, *, suffix: str
) -> Check:
    """Запрос, возвращающий узлы, у которых число связей вышло за границу."""
    return Check(
        name=f"{relation}.{side}.{suffix}",
        subject=subject,
        sql=(
            "SELECT n.id, n.kind, count(l.id) AS actual\n"
            "FROM node n\n"
            f"LEFT JOIN link l ON l.{column} = n.id AND l.rel = '{relation}'\n"
            "  AND l.retired_at IS NULL\n"
            f"WHERE n.retired_at IS NULL AND n.kind IN ({kinds})\n"
            "GROUP BY n.id, n.kind\n"
            f"HAVING count(l.id) {condition};"
        ),
    )


def _acyclicity_checks(descriptor: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []
    for relation in descriptor["relations"]:
        if not relation["acyclic"]:
            continue
        checks.append(
            Check(
                name=f"{relation['name']}.acyclic",
                subject=f"цикл по связи {relation['name']}",
                sql=(
                    "WITH RECURSIVE walk(root, node, depth) AS (\n"
                    "  SELECT src_id, dst_id, 1 FROM link\n"
                    f"   WHERE rel = '{relation['name']}' AND retired_at IS NULL\n"
                    "  UNION ALL\n"
                    "  SELECT w.root, l.dst_id, w.depth + 1\n"
                    "  FROM walk w\n"
                    f"  JOIN link l ON l.src_id = w.node AND l.rel = '{relation['name']}'\n"
                    "   AND l.retired_at IS NULL\n"
                    "  WHERE w.depth < 64\n"
                    ")\n"
                    "SELECT DISTINCT root FROM walk WHERE node = root;"
                ),
            )
        )
    return checks


def suspect_links_check() -> Check:
    """Связи, подтверждённые против устаревшей ревизии цели."""
    return Check(
        name="links.suspect",
        subject="связь подтверждена против устаревшей ревизии цели",
        sql=(
            "SELECT l.id, l.rel, l.conf_rev, max(r.rev) AS current_rev\n"
            "FROM link l\n"
            "JOIN node_revision r ON r.node_id = l.dst_id\n"
            "WHERE l.retired_at IS NULL\n"
            "GROUP BY l.id, l.rel, l.conf_rev\n"
            "HAVING l.conf_rev < max(r.rev);"
        ),
    )


def run_checks(checks: Iterable[Check], fetch: Fetch) -> list[Violation]:
    """Выполнить проверки и вернуть только те, что нашли нарушения."""
    violations: list[Violation] = []
    for check in checks:
        rows = fetch(check.sql)
        if rows:
            violations.append(Violation(check=check.name, subject=check.subject, rows=rows))
    return violations
