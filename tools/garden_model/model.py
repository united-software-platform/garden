"""Объектное представление модели и её чтение из YAML.

Модуль отвечает только за формат: структуру файлов, обязательные ключи, типы
значений. Смысловые проверки — дубли номеров, ссылки на несуществующие
элементы, пересечение с занятыми номерами — живут в `validate`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

import yaml

from .errors import ModelError

SCALAR_TYPES = ("text", "int", "bool", "timestamp", "uuid")
CODE_RE = re.compile(r"^[A-Z]{2,10}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
ENUM_VALUE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True, order=True)
class Version:
    """Версия метамодели: старший разряд — контракт, средний — структура, младший — описания."""

    major: int
    minor: int
    patch: int

    @staticmethod
    def parse(raw: Any, *, where: str) -> "Version":
        if not isinstance(raw, str):
            raise ModelError("версия должна быть строкой вида 1.2.0", where=where)
        parts = raw.split(".")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ModelError(f"версия {raw!r} не имеет вида 1.2.0", where=where)
        return Version(int(parts[0]), int(parts[1]), int(parts[2]))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class TypeRef:
    """Ссылка на тип значения поля: скаляр, перечисление или узел."""

    kind: str  # scalar | enum | ref
    name: str

    @staticmethod
    def parse(raw: Any, *, where: str) -> "TypeRef":
        if not isinstance(raw, str) or not raw:
            raise ModelError("тип поля должен быть непустой строкой", where=where)
        if raw in SCALAR_TYPES:
            return TypeRef("scalar", raw)
        for prefix, kind in (("enum.", "enum"), ("ref.", "ref")):
            if raw.startswith(prefix):
                target = raw[len(prefix):]
                if not target:
                    raise ModelError(f"после {prefix!r} не указано имя", where=where)
                return TypeRef(kind, target)
        raise ModelError(
            f"неизвестный тип {raw!r}: ожидается один из {', '.join(SCALAR_TYPES)}"
            " либо enum.<имя> / ref.<ТИП>",
            where=where,
        )

    def __str__(self) -> str:
        return self.name if self.kind == "scalar" else f"{self.kind}.{self.name}"


@dataclass(frozen=True)
class Taken:
    """Занятые навсегда номера, имена и коды. Пополняется инструментом при удалении."""

    fields: tuple[int, ...] = ()
    names: tuple[str, ...] = ()
    codes: tuple[int, ...] = ()

    @staticmethod
    def parse(raw: Any, *, where: str) -> "Taken":
        if raw is None:
            return Taken()
        if not isinstance(raw, dict):
            raise ModelError("раздел taken должен быть отображением", where=where)
        unknown = set(raw) - {"fields", "names", "codes"}
        if unknown:
            raise ModelError(f"неизвестные ключи taken: {', '.join(sorted(unknown))}", where=where)
        return Taken(
            fields=tuple(_int_list(raw.get("fields"), where=f"{where}.taken.fields")),
            names=tuple(_str_list(raw.get("names"), where=f"{where}.taken.names")),
            codes=tuple(_int_list(raw.get("codes"), where=f"{where}.taken.codes")),
        )


@dataclass(frozen=True)
class Field:
    """Поле типа узла. Идентичность — номер, имя может меняться."""

    id: int
    name: str
    type: TypeRef
    required: bool = False
    required_since: int | None = None
    description: str | None = None


@dataclass(frozen=True)
class NodeType:
    """Тип узла графа: бизнес-требование, функциональное требование, задача и прочие."""

    id: int
    code: str
    name_ru: str
    name_en: str
    fields: tuple[Field, ...]
    taken: Taken = Taken()
    description: str | None = None

    @property
    def table(self) -> str:
        """Имя таблицы ревизий, выводимое из кода типа."""
        return f"{self.code.lower()}_revision"


@dataclass(frozen=True)
class EnumValue:
    id: int
    name: str


@dataclass(frozen=True)
class EnumDef:
    id: int
    name: str
    values: tuple[EnumValue, ...]
    taken: Taken = Taken()


@dataclass(frozen=True)
class Cardinality:
    """Границы числа связей. `max = None` означает «без верхней границы»."""

    min: int = 0
    max: int | None = None

    @staticmethod
    def parse(raw: Any, *, where: str) -> "Cardinality":
        if raw is None:
            return Cardinality()
        if not isinstance(raw, dict):
            raise ModelError("кардинальность должна быть отображением min/max", where=where)
        unknown = set(raw) - {"min", "max"}
        if unknown:
            raise ModelError(f"неизвестные ключи: {', '.join(sorted(unknown))}", where=where)
        low = raw.get("min", 0)
        high = raw.get("max", None)
        if not isinstance(low, int) or low < 0:
            raise ModelError("min должен быть целым не меньше нуля", where=where)
        if high is not None and (not isinstance(high, int) or high < 1):
            raise ModelError("max должен быть целым не меньше единицы либо отсутствовать", where=where)
        if high is not None and high < low:
            raise ModelError("max меньше min", where=where)
        return Cardinality(low, high)


@dataclass(frozen=True)
class Relation:
    """Сигнатура связи: между какими типами она допустима и в каком числе."""

    id: int
    name: str
    sources: tuple[str, ...]
    targets: tuple[str, ...]
    source_cardinality: Cardinality = Cardinality()
    target_cardinality: Cardinality = Cardinality()
    acyclic: bool = True
    description: str | None = None


@dataclass(frozen=True)
class Model:
    """Модель целиком: перечисления, типы узлов и сигнатуры связей одной версии."""

    package: str
    version: Version
    enums: tuple[EnumDef, ...] = ()
    types: tuple[NodeType, ...] = ()
    relations: tuple[Relation, ...] = ()
    source: Path | None = dc_field(default=None, compare=False)

    def type_by_code(self, code: str) -> NodeType | None:
        return next((t for t in self.types if t.code == code), None)

    def enum_by_name(self, name: str) -> EnumDef | None:
        return next((e for e in self.enums if e.name == name), None)


# --- чтение ---------------------------------------------------------------


def load_model(root: Path) -> Model:
    """Прочитать модель из каталога: `_manifest.yaml`, `types/*.yaml`, `relations.yaml`."""
    root = Path(root)
    if not root.is_dir():
        raise ModelError("каталог модели не найден", where=str(root))

    manifest = _read_yaml(root / "_manifest.yaml")
    _require_keys(manifest, {"model", "version"}, allowed={"model", "version", "enums"}, where="_manifest.yaml")

    package = manifest["model"]
    if not isinstance(package, str) or not package:
        raise ModelError("ключ model должен быть непустой строкой", where="_manifest.yaml")
    version = Version.parse(manifest["version"], where="_manifest.yaml")
    enums = tuple(_parse_enums(manifest.get("enums")))

    types_dir = root / "types"
    types: list[NodeType] = []
    if types_dir.is_dir():
        for path in sorted(types_dir.glob("*.yaml")):
            types.append(_parse_type(_read_yaml(path), where=f"types/{path.name}"))

    taken_path = root / "_taken.yaml"
    if taken_path.exists():
        extra = _read_yaml(taken_path)
        _require_keys(extra, set(), allowed={"types", "enums"}, where="_taken.yaml")
        types = [_merge_taken(t, (extra.get("types") or {}).get(t.code), where="_taken.yaml") for t in types]
        enums = tuple(
            _merge_taken(e, (extra.get("enums") or {}).get(e.name), where="_taken.yaml") for e in enums
        )

    relations_path = root / "relations.yaml"
    relations: tuple[Relation, ...] = ()
    if relations_path.exists():
        raw = _read_yaml(relations_path)
        _require_keys(raw, {"relations"}, allowed={"relations"}, where="relations.yaml")
        relations = tuple(
            _parse_relation(item, where=f"relations.yaml[{i}]")
            for i, item in enumerate(_as_list(raw["relations"], where="relations.yaml"))
        )

    return Model(
        package=package,
        version=version,
        enums=enums,
        types=tuple(sorted(types, key=lambda t: t.id)),
        relations=tuple(sorted(relations, key=lambda r: r.id)),
        source=root,
    )


def _merge_taken(element, extra: Any, *, where: str):
    """Соединить занятые номера из файла элемента и из файла, который ведёт инструмент."""
    if extra is None:
        return element
    added = Taken.parse(extra, where=where)
    from dataclasses import replace

    return replace(
        element,
        taken=Taken(
            fields=tuple(sorted(set(element.taken.fields) | set(added.fields))),
            names=tuple(sorted(set(element.taken.names) | set(added.names))),
            codes=tuple(sorted(set(element.taken.codes) | set(added.codes))),
        ),
    )


def _parse_enums(raw: Any) -> list[EnumDef]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise ModelError("раздел enums должен быть отображением имя -> описание", where="_manifest.yaml")
    result: list[EnumDef] = []
    for name, body in raw.items():
        where = f"_manifest.yaml.enums.{name}"
        if not isinstance(body, dict):
            raise ModelError("описание перечисления должно быть отображением", where=where)
        _require_keys(body, {"id", "values"}, allowed={"id", "values", "taken"}, where=where)
        values = tuple(
            EnumValue(
                id=_int(item.get("id"), where=f"{where}.values[{i}].id"),
                name=_pattern(item.get("name"), ENUM_VALUE_RE, "ЗАГЛАВНЫМИ_С_ПОДЧЁРКИВАНИЕМ", where=f"{where}.values[{i}].name"),
            )
            for i, item in enumerate(_as_dict_list(body["values"], where=f"{where}.values"))
        )
        result.append(
            EnumDef(
                id=_int(body["id"], where=f"{where}.id"),
                name=str(name),
                values=tuple(sorted(values, key=lambda v: v.id)),
                taken=Taken.parse(body.get("taken"), where=where),
            )
        )
    return sorted(result, key=lambda e: e.id)


def _parse_type(raw: Any, *, where: str) -> NodeType:
    if not isinstance(raw, dict):
        raise ModelError("описание типа должно быть отображением", where=where)
    _require_keys(
        raw,
        {"type", "id", "name", "fields"},
        allowed={"type", "id", "name", "fields", "taken", "description"},
        where=where,
    )
    code = _pattern(raw["type"], CODE_RE, "2-10 заглавных латинских букв", where=f"{where}.type")
    names = raw["name"]
    if not isinstance(names, dict) or "ru" not in names or "en" not in names:
        raise ModelError("name должен содержать ключи ru и en", where=f"{where}.name")

    fields = tuple(
        _parse_field(item, where=f"{where}.fields[{i}]")
        for i, item in enumerate(_as_dict_list(raw["fields"], where=f"{where}.fields"))
    )
    return NodeType(
        id=_int(raw["id"], where=f"{where}.id"),
        code=code,
        name_ru=str(names["ru"]),
        name_en=str(names["en"]),
        fields=tuple(sorted(fields, key=lambda f: f.id)),
        taken=Taken.parse(raw.get("taken"), where=where),
        description=raw.get("description"),
    )


def _parse_field(raw: dict[str, Any], *, where: str) -> Field:
    _require_keys(
        raw,
        {"id", "name", "type"},
        allowed={"id", "name", "type", "required", "required_since", "description"},
        where=where,
    )
    required = raw.get("required", False)
    if not isinstance(required, bool):
        raise ModelError("required должен быть true или false", where=f"{where}.required")
    since = raw.get("required_since")
    if since is not None and (not isinstance(since, int) or since < 0):
        raise ModelError("required_since должен быть средним разрядом версии", where=f"{where}.required_since")
    if since is not None and not required:
        raise ModelError("required_since указан у необязательного поля", where=where)
    return Field(
        id=_int(raw["id"], where=f"{where}.id"),
        name=_pattern(raw["name"], NAME_RE, "строчными латинскими с подчёркиваниями", where=f"{where}.name"),
        type=TypeRef.parse(raw["type"], where=f"{where}.type"),
        required=required,
        required_since=since,
        description=raw.get("description"),
    )


def _parse_relation(raw: Any, *, where: str) -> Relation:
    if not isinstance(raw, dict):
        raise ModelError("описание связи должно быть отображением", where=where)
    _require_keys(
        raw,
        {"id", "name", "from", "to"},
        allowed={"id", "name", "from", "to", "from_cardinality", "to_cardinality", "acyclic", "description"},
        where=where,
    )
    sources = _str_list(raw["from"], where=f"{where}.from")
    targets = _str_list(raw["to"], where=f"{where}.to")
    if not sources:
        raise ModelError("не указан домен связи (from)", where=where)
    if not targets:
        raise ModelError("не указан кодомен связи (to)", where=where)
    acyclic = raw.get("acyclic", True)
    if not isinstance(acyclic, bool):
        raise ModelError("acyclic должен быть true или false", where=f"{where}.acyclic")
    return Relation(
        id=_int(raw["id"], where=f"{where}.id"),
        name=_pattern(raw["name"], NAME_RE, "строчными латинскими с подчёркиваниями", where=f"{where}.name"),
        sources=tuple(sources),
        targets=tuple(targets),
        source_cardinality=Cardinality.parse(raw.get("from_cardinality"), where=f"{where}.from_cardinality"),
        target_cardinality=Cardinality.parse(raw.get("to_cardinality"), where=f"{where}.to_cardinality"),
        acyclic=acyclic,
        description=raw.get("description"),
    )


# --- вспомогательное ------------------------------------------------------


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        raise ModelError("файл не найден", where=str(path))
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ModelError(f"не разбирается как YAML: {exc}", where=str(path)) from exc
    if data is None:
        raise ModelError("файл пуст", where=str(path))
    return data


def _require_keys(raw: Any, required: set[str], *, allowed: set[str], where: str) -> None:
    if not isinstance(raw, dict):
        raise ModelError("ожидалось отображение", where=where)
    missing = required - set(raw)
    if missing:
        raise ModelError(f"отсутствуют обязательные ключи: {', '.join(sorted(missing))}", where=where)
    unknown = set(raw) - allowed
    if unknown:
        raise ModelError(f"неизвестные ключи: {', '.join(sorted(unknown))}", where=where)


def _as_list(raw: Any, *, where: str) -> list[Any]:
    if not isinstance(raw, list):
        raise ModelError("ожидался список", where=where)
    return raw


def _as_dict_list(raw: Any, *, where: str) -> list[dict[str, Any]]:
    items = _as_list(raw, where=where)
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ModelError("элемент списка должен быть отображением", where=f"{where}[{i}]")
    return items


def _int(raw: Any, *, where: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        raise ModelError("ожидалось целое число не меньше единицы", where=where)
    return raw


def _int_list(raw: Any, *, where: str) -> list[int]:
    if raw is None:
        return []
    return [_int(item, where=f"{where}[{i}]") for i, item in enumerate(_as_list(raw, where=where))]


def _str_list(raw: Any, *, where: str) -> list[str]:
    if raw is None:
        return []
    items = _as_list(raw, where=where)
    for i, item in enumerate(items):
        if not isinstance(item, str) or not item:
            raise ModelError("ожидалась непустая строка", where=f"{where}[{i}]")
    return list(items)


def _pattern(raw: Any, regex: re.Pattern[str], expectation: str, *, where: str) -> str:
    if not isinstance(raw, str) or not regex.match(raw):
        raise ModelError(f"значение {raw!r} должно записываться {expectation}", where=where)
    return raw
