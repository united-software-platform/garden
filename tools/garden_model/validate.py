"""Смысловые проверки модели: дубли, пересечения с занятыми номерами, ссылки."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .errors import ModelError
from .model import Model, NodeType


def validate_model(model: Model) -> None:
    """Проверить модель целиком. Первое же нарушение поднимает `ModelError`.

    Проверки внутри одной версии. Сравнение с ранее выпущенной версией — не здесь,
    а в сравнении дескрипторов: для него нужны две версии.
    """
    _check_unique([t.id for t in model.types], "номер типа", where="model")
    _check_unique([t.code for t in model.types], "код типа", where="model")
    _check_unique([e.id for e in model.enums], "номер перечисления", where="model")
    _check_unique([r.id for r in model.relations], "номер связи", where="model")
    _check_unique([r.name for r in model.relations], "имя связи", where="model")

    for enum in model.enums:
        where = f"enum {enum.name}"
        _check_unique([v.id for v in enum.values], "номер значения", where=where)
        _check_unique([v.name for v in enum.values], "имя значения", where=where)
        _check_taken([v.id for v in enum.values], enum.taken.fields, "номер значения", where=where)
        _check_taken([v.name for v in enum.values], enum.taken.names, "имя значения", where=where)

    for node in model.types:
        _validate_type(model, node)

    codes = {t.code for t in model.types}
    for relation in model.relations:
        where = f"связь {relation.name}"
        for code in relation.sources + relation.targets:
            if code not in codes:
                raise ModelError(f"ссылка на несуществующий тип узла {code!r}", where=where)


def _validate_type(model: Model, node: NodeType) -> None:
    where = f"тип {node.code}"
    numbers = [f.id for f in node.fields]
    names = [f.name for f in node.fields]

    _check_unique(numbers, "номер поля", where=where)
    _check_unique(names, "имя поля", where=where)
    _check_taken(numbers, node.taken.fields, "номер поля", where=where)
    _check_taken(names, node.taken.names, "имя поля", where=where)

    for field in node.fields:
        field_where = f"{where}, поле {field.id} ({field.name})"
        if field.type.kind == "enum" and model.enum_by_name(field.type.name) is None:
            raise ModelError(
                f"ссылка на несуществующее перечисление {field.type.name!r}", where=field_where
            )
        if field.type.kind == "ref" and model.type_by_code(field.type.name) is None:
            raise ModelError(
                f"ссылка на несуществующий тип узла {field.type.name!r}", where=field_where
            )
        if field.required_since is not None and field.required_since > model.version.minor:
            raise ModelError(
                f"required_since {field.required_since} больше текущего среднего разряда "
                f"{model.version.minor}",
                where=field_where,
            )


def _check_unique(values: list[Any], subject: str, *, where: str) -> None:
    seen: set[Any] = set()
    for value in values:
        if value in seen:
            raise ModelError(f"{subject} {value!r} встречается дважды", where=where)
        seen.add(value)


def _check_taken(active: list[Any], taken: Iterable[Any], subject: str, *, where: str) -> None:
    occupied = set(taken)
    for value in active:
        if value in occupied:
            raise ModelError(
                f"{subject} {value!r} числится занятым и не может быть выдан повторно",
                where=where,
            )
