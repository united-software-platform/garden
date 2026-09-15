"""Сравнение двух дескрипторов и классификация полученных операций.

Сравнение идёт по номерам элементов: имя и порядок на результат не влияют.
Именно поэтому переименование опознаётся как переименование, а не как удаление
одного поля и добавление другого — и данные не теряются.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

SAFE = "safe"
BREAKING = "breaking"
DESTRUCTIVE = "destructive"

#: Человекочитаемые пояснения к классам операций.
CLASS_LABEL = {
    SAFE: "безопасно",
    BREAKING: "ломает контракт",
    DESTRUCTIVE: "удаляет данные",
}


@dataclass(frozen=True)
class Change:
    """Одна операция дельты в терминах модели."""

    op: str
    element: str
    kind: str
    detail: str
    payload: dict[str, Any]

    def __str__(self) -> str:
        return f"{self.element}: {self.detail} [{CLASS_LABEL[self.kind]}]"


def diff_descriptors(old: dict[str, Any], new: dict[str, Any]) -> list[Change]:
    """Собрать дельту перехода `old -> new`."""
    changes: list[Change] = []
    changes += _diff_enums(old.get("enums", []), new.get("enums", []))
    changes += _diff_types(old.get("types", []), new.get("types", []))
    changes += _diff_relations(old.get("relations", []), new.get("relations", []))
    return changes


def worst_kind(changes: Iterable[Change]) -> str:
    """Самый тяжёлый класс среди операций дельты."""
    kinds = {c.kind for c in changes}
    for kind in (DESTRUCTIVE, BREAKING, SAFE):
        if kind in kinds:
            return kind
    return SAFE


def _by_id(items: Iterable[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {item["id"]: item for item in items}


def _diff_enums(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[Change]:
    changes: list[Change] = []
    old_map, new_map = _by_id(old), _by_id(new)

    for enum_id, entry in new_map.items():
        if enum_id not in old_map:
            changes.append(
                Change("enum_added", entry["name"], SAFE, "добавлено перечисление", {"enum": entry})
            )
            continue
        before, after = _by_id(old_map[enum_id]["values"]), _by_id(entry["values"])
        for value_id, value in after.items():
            if value_id not in before:
                changes.append(
                    Change(
                        "enum_value_added",
                        f"{entry['name']}.{value['name']}",
                        SAFE,
                        f"добавлено значение {value['name']}",
                        {"enum": entry["name"], "value": value},
                    )
                )
            elif before[value_id]["name"] != value["name"]:
                changes.append(
                    Change(
                        "enum_value_renamed",
                        f"{entry['name']}.{value_id}",
                        SAFE,
                        f"значение {before[value_id]['name']} -> {value['name']}",
                        {"enum": entry["name"], "from": before[value_id], "to": value},
                    )
                )
        for value_id, value in before.items():
            if value_id not in after:
                changes.append(
                    Change(
                        "enum_value_removed",
                        f"{entry['name']}.{value['name']}",
                        BREAKING,
                        f"удалено значение {value['name']}",
                        {"enum": entry["name"], "value": value},
                    )
                )

    for enum_id, entry in old_map.items():
        if enum_id not in new_map:
            changes.append(
                Change(
                    "enum_removed", entry["name"], BREAKING, "удалено перечисление", {"enum": entry}
                )
            )
    return changes


def _diff_types(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[Change]:
    changes: list[Change] = []
    old_map, new_map = _by_id(old), _by_id(new)

    for type_id, entry in new_map.items():
        if type_id not in old_map:
            changes.append(
                Change("type_added", entry["code"], SAFE, "добавлен тип узла", {"type": entry})
            )
            continue
        changes += _diff_fields(old_map[type_id], entry)

    for type_id, entry in old_map.items():
        if type_id not in new_map:
            changes.append(
                Change(
                    "type_removed",
                    entry["code"],
                    DESTRUCTIVE,
                    f"удалён тип узла, таблица {entry['table']} будет удалена вместе с данными",
                    {"type": entry},
                )
            )
    return changes


def _diff_fields(old_type: dict[str, Any], new_type: dict[str, Any]) -> list[Change]:
    code = new_type["code"]
    changes: list[Change] = []
    before, after = _by_id(old_type["fields"]), _by_id(new_type["fields"])

    for field_id, field in after.items():
        element = f"{code}.{field_id}"
        if field_id not in before:
            kind = BREAKING if field["required"] and not field["required_since"] else SAFE
            detail = f"добавлено поле {field['name']}"
            if kind is BREAKING:
                detail += " как обязательное без required_since"
            changes.append(
                Change("field_added", element, kind, detail, {"type": code, "field": field})
            )
            continue

        was = before[field_id]
        if was["name"] != field["name"]:
            changes.append(
                Change(
                    "field_renamed",
                    element,
                    SAFE,
                    f"{was['name']} -> {field['name']}",
                    {"type": code, "from": was, "to": field},
                )
            )
        if (was["kind"], was["type"]) != (field["kind"], field["type"]):
            changes.append(
                Change(
                    "field_type_changed",
                    element,
                    DESTRUCTIVE,
                    f"тип поля {field['name']}: {was['type']} -> {field['type']}",
                    {"type": code, "from": was, "to": field},
                )
            )
        if not was["required"] and field["required"]:
            kind = SAFE if field["required_since"] else BREAKING
            changes.append(
                Change(
                    "field_required",
                    element,
                    kind,
                    f"поле {field['name']} стало обязательным"
                    + (
                        f" начиная с {field['required_since']}"
                        if field["required_since"]
                        else " без required_since"
                    ),
                    {"type": code, "field": field},
                )
            )

    for field_id, field in before.items():
        if field_id not in after:
            changes.append(
                Change(
                    "field_removed",
                    f"{code}.{field_id}",
                    DESTRUCTIVE,
                    f"удалено поле {field['name']}, данные колонки {field['column']} будут удалены",
                    {"type": code, "field": field},
                )
            )
    return changes


def _diff_relations(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[Change]:
    changes: list[Change] = []
    old_map, new_map = _by_id(old), _by_id(new)

    for relation_id, entry in new_map.items():
        if relation_id not in old_map:
            changes.append(
                Change(
                    "relation_added", entry["name"], SAFE, "добавлена связь", {"relation": entry}
                )
            )
            continue
        was = old_map[relation_id]
        if was["name"] != entry["name"]:
            changes.append(
                Change(
                    "relation_renamed",
                    str(relation_id),
                    SAFE,
                    f"связь {was['name']} -> {entry['name']}",
                    {"from": was, "to": entry},
                )
            )
        for side in ("from", "to"):
            removed = [c for c in was[side] if c not in entry[side]]
            added = [c for c in entry[side] if c not in was[side]]
            if added:
                changes.append(
                    Change(
                        f"relation_{side}_widened",
                        entry["name"],
                        SAFE,
                        f"{side}: добавлены типы {', '.join(added)}",
                        {"relation": entry, "codes": added},
                    )
                )
            if removed:
                changes.append(
                    Change(
                        f"relation_{side}_narrowed",
                        entry["name"],
                        BREAKING,
                        f"{side}: удалены типы {', '.join(removed)}",
                        {"relation": entry, "codes": removed},
                    )
                )
        for side in ("from_cardinality", "to_cardinality"):
            changes += _diff_cardinality(entry["name"], side, was[side], entry[side])

    for relation_id, entry in old_map.items():
        if relation_id not in new_map:
            changes.append(
                Change(
                    "relation_removed",
                    entry["name"],
                    DESTRUCTIVE,
                    "удалена связь, её рёбра будут удалены",
                    {"relation": entry},
                )
            )
    return changes


def _diff_cardinality(
    name: str, side: str, was: dict[str, Any], now: dict[str, Any]
) -> list[Change]:
    if was == now:
        return []
    tightened = now["min"] > was["min"] or _narrower_max(was["max"], now["max"])
    kind = BREAKING if tightened else SAFE
    verb = "ужесточена" if tightened else "ослаблена"
    return [
        Change(
            "relation_cardinality",
            f"{name}.{side}",
            kind,
            f"{side}: кардинальность {verb} ({_card(was)} -> {_card(now)})",
            {"relation": name, "side": side, "from": was, "to": now},
        )
    ]


def _narrower_max(was: int | None, now: int | None) -> bool:
    if now is None:
        return False
    return was is None or now < was


def _card(value: dict[str, Any]) -> str:
    return f"{value['min']}..{'*' if value['max'] is None else value['max']}"
