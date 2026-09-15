"""Нейтральные операции над схемой, выводимые из дельты модели.

Операции не знают ни диалекта, ни имени файла: они называют, что должно измениться.
Диалект добавляет бэкенд, имя и место файла — раскладка.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ..diff import Change
from ..errors import ModelError


class Backend(Protocol):
    """Бэкенд СУБД: превращает нейтральные операции в диалект.

    Протокол описывает ровно то, что от бэкенда требует раскладка, и позволяет
    передавать модуль бэкенда как значение — второй бэкенд появится рядом и подойдёт
    под тот же протокол без правок раскладки.
    """

    NAME: str

    def render(self, op: Operation) -> tuple[list[str], list[str]]: ...

    def close_version(
        self, previous_hash: str | None, version: str, model_hash: str
    ) -> tuple[list[str], list[str], str | None]: ...

    def baseline_statements(self, descriptor: dict[str, Any]) -> list[str]: ...


#: Каталог для элементов, не принадлежащих ни одному типу узла.
CORE = "core"
#: Каталог для сигнатур связей.
RELATIONS = "rel"


@dataclass(frozen=True)
class Operation:
    """Одна операция над схемой.

    `folder` — каталог элемента модели, которому операция принадлежит;
    `slug` — часть идентификатора changeset'а, выводимая из координат модели.
    """

    kind: str
    folder: str
    slug: str
    comment: str
    payload: dict[str, Any]


def operations_for(change: Change) -> list[Operation]:
    """Развернуть операцию дельты в операции над схемой."""
    handler = _HANDLERS.get(change.op)
    return handler(change) if handler else []


def _type_folder(code: str) -> str:
    return code.lower()


#: Суффикс идентификатора changeset'а для каждой операции над колонкой.
COLUMN_SLUG = {
    "add_column": "add",
    "drop_column": "drop",
    "rename_column": "rename",
    "alter_column_type": "retype",
}


def _field_ops(change: Change, kind: str) -> list[Operation]:
    code = change.payload["type"]
    field = change.payload.get("field") or change.payload["to"]
    return [
        Operation(
            kind=kind,
            folder=_type_folder(code),
            slug=f"{code}.f{field['id']}.{COLUMN_SLUG[kind]}",
            comment=change.detail,
            payload={"type": code, "field": field, "from": change.payload.get("from")},
        )
    ]


def _added(change: Change) -> list[Operation]:
    ops = _field_ops(change, "add_column")
    field = change.payload["field"]
    if field["required"] and field["required_since"]:
        ops.append(_require_op(change.payload["type"], field, change.detail))
    return ops


def _required(change: Change) -> list[Operation]:
    field = change.payload["field"]
    return [_require_op(change.payload["type"], field, change.detail)]


def _require_op(code: str, field: dict[str, Any], comment: str) -> Operation:
    return Operation(
        kind="require_since",
        folder=_type_folder(code),
        slug=f"{code}.f{field['id']}.require",
        comment=comment,
        payload={"type": code, "field": field},
    )


def _type_added(change: Change) -> list[Operation]:
    entry = change.payload["type"]
    return [
        Operation(
            "create_table",
            _type_folder(entry["code"]),
            f"{entry['code']}.create",
            change.detail,
            {"type": entry},
        ),
        Operation(
            "add_node_kind",
            CORE,
            f"kind.{entry['code']}.add",
            f"тип узла {entry['code']} добавлен в перечень видов узлов",
            {"type": entry},
        ),
    ]


def _type_removed(change: Change) -> list[Operation]:
    entry = change.payload["type"]
    return [
        Operation(
            "drop_table",
            _type_folder(entry["code"]),
            f"{entry['code']}.drop",
            change.detail,
            {"type": entry},
        )
    ]


def _relation_op(kind: str, suffix: str) -> Callable[[Change], list[Operation]]:
    def build(change: Change) -> list[Operation]:
        raw = change.payload.get("relation")
        # Дельта называет связь либо описанием, либо одним именем — например, когда
        # сигнатура удалена и описывать больше нечего.
        entry = {"name": raw} if isinstance(raw, str) else raw
        if not isinstance(entry, dict):
            raise ModelError(
                f"операция {kind} не называет связь", where=f"дельта: {change.element}"
            )
        return [
            Operation(
                kind,
                RELATIONS,
                f"rel.{entry['name']}.{suffix}",
                change.detail,
                {"relation": entry, "codes": change.payload.get("codes", [])},
            )
        ]

    return build


def _enum_value_added(change: Change) -> list[Operation]:
    return [
        Operation(
            "add_enum_value",
            CORE,
            f"enum.{change.payload['enum']}.{change.payload['value']['name']}",
            change.detail,
            dict(change.payload),
        )
    ]


_HANDLERS = {
    "field_added": _added,
    "field_removed": lambda c: _field_ops(c, "drop_column"),
    "field_renamed": lambda c: _field_ops(c, "rename_column"),
    "field_type_changed": lambda c: _field_ops(c, "alter_column_type"),
    "field_required": _required,
    "type_added": _type_added,
    "type_removed": _type_removed,
    "enum_value_added": _enum_value_added,
    "relation_added": _relation_op("insert_signature", "add"),
    "relation_removed": _relation_op("delete_signature", "drop"),
    "relation_from_widened": _relation_op("update_signature", "from_widened"),
    "relation_to_widened": _relation_op("update_signature", "to_widened"),
    "relation_from_narrowed": _relation_op("update_signature", "from_narrowed"),
    "relation_to_narrowed": _relation_op("update_signature", "to_narrowed"),
    "relation_cardinality": _relation_op("update_signature", "cardinality"),
}
