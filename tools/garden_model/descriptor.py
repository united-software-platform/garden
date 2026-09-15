"""Компиляция модели в дескриптор — самодостаточное описание контракта.

Дескриптор отличается от модели тем, что в нём раскрыты умолчания, разрешены ссылки
на перечисления и вычислены производные имена. Его чтение не требует доступа к исходным
файлам модели, поэтому именно он передаётся потребителям.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .errors import ModelError
from .model import EnumDef, Field, Model, NodeType, Relation, Taken
from .validate import validate_model

#: Ключи, не входящие в хеш: их изменение не меняет ни структуру, ни данные.
#: Перечень занятых номеров исключён: он не описывает схему хранилища, а пополняется
#: инструментом после подсчёта дескриптора — иначе хеш модели расходился бы с хешем,
#: записанным в хранилище замыкающей миграцией.
#: Номер версии тоже исключён — иначе повышение версии само по себе меняло бы хеш,
#: и случай «версию повысили, а модель не тронули» стал бы неотличим от изменения.
NON_STRUCTURAL = frozenset(
    {"description", "name_ru", "name_en", "hash", "version", "major", "minor", "patch", "taken"}
)


def compile_model(model: Model) -> dict[str, Any]:
    """Собрать дескриптор модели и посчитать его хеш."""
    validate_model(model)

    body: dict[str, Any] = {
        "model": model.package,
        "version": str(model.version),
        "major": model.version.major,
        "minor": model.version.minor,
        "patch": model.version.patch,
        "enums": [_enum(e) for e in model.enums],
        "types": [_type(model, t) for t in model.types],
        "relations": [_relation(r) for r in model.relations],
    }
    body["hash"] = (
        "sha256:" + hashlib.sha256(canonical_json(_structural(body)).encode("utf-8")).hexdigest()
    )
    return body


def canonical_json(value: Any) -> str:
    """Каноническое представление: отсортированные ключи, стабильные разделители."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def dump_descriptor(descriptor: dict[str, Any]) -> str:
    """Текст файла дескриптора: читаемый отступ, стабильный порядок, перевод строки в конце."""
    return json.dumps(descriptor, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _structural(value: Any) -> Any:
    """Убрать из структуры всё, что не влияет ни на схему, ни на данные."""
    if isinstance(value, dict):
        return {k: _structural(v) for k, v in value.items() if k not in NON_STRUCTURAL}
    if isinstance(value, list):
        return [_structural(item) for item in value]
    return value


def _enum(enum: EnumDef) -> dict[str, Any]:
    return {
        "id": enum.id,
        "name": enum.name,
        "values": [{"id": v.id, "name": v.name} for v in enum.values],
        "taken": _taken(enum.taken),
    }


def _type(model: Model, node: NodeType) -> dict[str, Any]:
    return {
        "id": node.id,
        "code": node.code,
        "table": node.table,
        "code_pattern": f"{node.code}-{{NNN}}",
        "name_ru": node.name_ru,
        "name_en": node.name_en,
        "description": node.description,
        "fields": [_field(model, f) for f in node.fields],
        "taken": _taken(node.taken),
    }


def _field(model: Model, field: Field) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": field.id,
        "name": field.name,
        "column": field.name,
        "kind": field.type.kind,
        "type": field.type.name,
        "required": field.required,
        # Обязательное поле без явного признака обязательно с самого начала.
        "required_since": (field.required_since or 0) if field.required else None,
        "description": field.description,
    }
    if field.type.kind == "enum":
        enum = model.enum_by_name(field.type.name)
        if enum is None:
            # Ссылку на несуществующее перечисление отвергает validate_model; проверка
            # здесь закрывает случай вызова компиляции в обход валидации.
            raise ModelError(
                f"поле ссылается на неизвестное перечисление {field.type.name!r}",
                where=f"поле {field.name}",
            )
        entry["values"] = [v.name for v in enum.values]
    return entry


def _relation(relation: Relation) -> dict[str, Any]:
    return {
        "id": relation.id,
        "name": relation.name,
        "from": list(relation.sources),
        "to": list(relation.targets),
        "from_cardinality": {
            "min": relation.source_cardinality.min,
            "max": relation.source_cardinality.max,
        },
        "to_cardinality": {
            "min": relation.target_cardinality.min,
            "max": relation.target_cardinality.max,
        },
        "acyclic": relation.acyclic,
        "description": relation.description,
    }


def _taken(taken: Taken) -> dict[str, Any]:
    return {"fields": list(taken.fields), "names": list(taken.names), "codes": list(taken.codes)}
