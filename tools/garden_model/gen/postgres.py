"""Бэкенд PostgreSQL: превращает нейтральные операции в диалект.

Модель диалекта не содержит — всё, что специфично для PostgreSQL, живёт здесь.
Второй бэкенд появится рядом и получит те же операции на вход.
"""

from __future__ import annotations

from typing import Any

from .ops import Operation

NAME = "postgres"

SCALAR_SQL = {
    "text": "text",
    "int": "integer",
    "bool": "boolean",
    "timestamp": "timestamptz",
    "uuid": "uuid",
}


def column_type(field: dict[str, Any]) -> str:
    """Тип колонки для поля дескриптора."""
    if field["kind"] == "enum":
        return field["type"]
    if field["kind"] == "ref":
        return "bigint"
    return SCALAR_SQL[field["type"]]


def render(op: Operation) -> tuple[list[str], list[str]]:
    """Вернуть пару «операторы, откат» для одной операции."""
    return _RENDERERS[op.kind](op.payload)


def _add_column(p: dict[str, Any]) -> tuple[list[str], list[str]]:
    table, field = _table(p["type"]), p["field"]
    return (
        [f'ALTER TABLE {table} ADD COLUMN {field["column"]} {column_type(field)};'],
        [f'ALTER TABLE {table} DROP COLUMN {field["column"]};'],
    )


def _drop_column(p: dict[str, Any]) -> tuple[list[str], list[str]]:
    table, field = _table(p["type"]), p["field"]
    return (
        [f'ALTER TABLE {table} DROP COLUMN {field["column"]};'],
        # Откат вернёт колонку, но не данные: удаление значений необратимо.
        [f'ALTER TABLE {table} ADD COLUMN {field["column"]} {column_type(field)};'],
    )


def _rename_column(p: dict[str, Any]) -> tuple[list[str], list[str]]:
    table, was, now = _table(p["type"]), p["from"], p["field"]
    return (
        [f'ALTER TABLE {table} RENAME COLUMN {was["column"]} TO {now["column"]};'],
        [f'ALTER TABLE {table} RENAME COLUMN {now["column"]} TO {was["column"]};'],
    )


def _alter_column_type(p: dict[str, Any]) -> tuple[list[str], list[str]]:
    table, was, now = _table(p["type"]), p["from"], p["field"]
    return (
        [
            f'ALTER TABLE {table} ALTER COLUMN {now["column"]} TYPE {column_type(now)} '
            f'USING {now["column"]}::{column_type(now)};'
        ],
        [
            f'ALTER TABLE {table} ALTER COLUMN {was["column"]} TYPE {column_type(was)} '
            f'USING {was["column"]}::{column_type(was)};'
        ],
    )


def _require_since(p: dict[str, Any]) -> tuple[list[str], list[str]]:
    table, field = _table(p["type"]), p["field"]
    name = f'{p["type"].lower()}_{field["column"]}_since_{field["required_since"]}'
    return (
        [
            f"ALTER TABLE {table} ADD CONSTRAINT {name}\n"
            f'  CHECK (mm_version < {field["required_since"]} OR {field["column"]} IS NOT NULL) NOT VALID;'
        ],
        [f"ALTER TABLE {table} DROP CONSTRAINT {name};"],
    )


def _create_table(p: dict[str, Any]) -> tuple[list[str], list[str]]:
    entry = p["type"]
    columns = [
        "  node_id    bigint not null",
        "  rev        integer not null",
        "  mm_version integer not null",
        f"  kind       node_kind not null default '{entry['code']}' check (kind = '{entry['code']}')",
    ]
    columns += [f'  {f["column"]:<10} {column_type(f)}' for f in entry["fields"]]
    body = ",\n".join(
        columns
        + [
            "  primary key (node_id, rev)",
            "  foreign key (node_id, rev, mm_version)"
            " references node_revision(node_id, rev, mm_version)",
            "  foreign key (node_id, kind) references node(id, kind)",
        ]
    )
    return ([f"CREATE TABLE {_table(entry['code'])} (\n{body}\n);"], [f"DROP TABLE {_table(entry['code'])};"])


def _drop_table(p: dict[str, Any]) -> tuple[list[str], list[str]]:
    return ([f"DROP TABLE {_table(p['type']['code'])};"], ["-- восстановление таблицы с данными невозможно"])


def _add_node_kind(p: dict[str, Any]) -> tuple[list[str], list[str]]:
    code = p["type"]["code"]
    return (
        [f"ALTER TYPE node_kind ADD VALUE IF NOT EXISTS '{code}';"],
        ["-- значение перечислимого типа PostgreSQL не удаляется"],
    )


def _add_enum_value(p: dict[str, Any]) -> tuple[list[str], list[str]]:
    return (
        [f"ALTER TYPE {p['enum']} ADD VALUE IF NOT EXISTS '{p['value']['name']}';"],
        ["-- значение перечислимого типа PostgreSQL не удаляется"],
    )


def _insert_signature(p: dict[str, Any]) -> tuple[list[str], list[str]]:
    return (signature_rows(p["relation"]), [f"DELETE FROM rel_signature WHERE rel = '{p['relation']['name']}';"])


def _delete_signature(p: dict[str, Any]) -> tuple[list[str], list[str]]:
    name = p["relation"]["name"]
    return (
        [f"DELETE FROM link WHERE rel = '{name}';", f"DELETE FROM rel_signature WHERE rel = '{name}';"],
        ["-- восстановление удалённых рёбер невозможно"],
    )


def _update_signature(p: dict[str, Any]) -> tuple[list[str], list[str]]:
    relation = p["relation"]
    name = relation["name"]
    statements = [f"DELETE FROM rel_signature WHERE rel = '{name}';"] + signature_rows(relation)
    return (statements, [f"-- прежняя сигнатура связи {name} восстанавливается предыдущей миграцией"])


def signature_rows(relation: dict[str, Any]) -> list[str]:
    """Строки справочника сигнатур: по одной на каждое сочетание домена и кодомена."""
    if "from" not in relation:
        return [f"-- сигнатура связи {relation['name']} описана в дескрипторе версии"]
    rows = []
    for source in relation["from"]:
        for target in relation["to"]:
            rows.append(
                "INSERT INTO rel_signature (rel, src_kind, dst_kind, src_min, src_max, dst_min, dst_max, acyclic)\n"
                f"  VALUES ('{relation['name']}', '{source}', '{target}', "
                f"{relation['from_cardinality']['min']}, {_null(relation['from_cardinality']['max'])}, "
                f"{relation['to_cardinality']['min']}, {_null(relation['to_cardinality']['max'])}, "
                f"{str(relation['acyclic']).lower()});"
            )
    return rows


def close_version(previous_hash: str | None, version: str, model_hash: str) -> tuple[list[str], list[str], str | None]:
    """Замыкающая миграция версии: предусловие на прежнее состояние и запись нового."""
    statements = [
        f"UPDATE model_state SET version = '{version}', hash = '{model_hash}';",
    ]
    rollback = ["-- откат версии выполняется откатом миграций этой версии"]
    if previous_hash is None:
        statements = [
            "INSERT INTO model_state (version, hash) VALUES "
            f"('{version}', '{model_hash}');"
        ]
        return statements, rollback, None
    precondition = f"SELECT count(*) FROM model_state WHERE hash = '{previous_hash}'"
    return statements, rollback, precondition


def baseline_statements(descriptor: dict[str, Any]) -> list[str]:
    """Полная схема, выведенная из дескриптора: свёртка модели, а не история изменений."""
    statements: list[str] = []
    for enum in descriptor["enums"]:
        values = ", ".join(f"'{v['name']}'" for v in enum["values"])
        statements.append(f"CREATE TYPE {enum['name']} AS ENUM ({values});")

    kinds = ", ".join(f"'{t['code']}'" for t in descriptor["types"]) or "'NONE'"
    statements.append(f"CREATE TYPE node_kind AS ENUM ({kinds});")

    statements.append(
        "CREATE TABLE model_state (\n"
        "  version    text not null,\n"
        "  hash       text not null\n"
        ");"
    )
    statements.append(
        "CREATE TABLE node (\n"
        "  id         bigint generated always as identity primary key,\n"
        "  kind       node_kind not null,\n"
        "  code_num   integer not null check (code_num between 0 and 999),\n"
        "  created_at timestamptz not null default now(),\n"
        "  retired_at timestamptz,\n"
        "  unique (kind, code_num),\n"
        "  unique (id, kind)\n"
        ");"
    )
    statements.append(
        "CREATE VIEW node_code AS\n"
        "  SELECT id, kind, code_num,\n"
        "         kind::text || '-' || lpad(code_num::text, 3, '0') AS code\n"
        "  FROM node;"
    )
    statements.append(
        "CREATE TABLE node_revision (\n"
        "  node_id    bigint not null references node(id),\n"
        "  rev        integer not null check (rev > 0),\n"
        "  mm_version integer not null,\n"
        "  created_at timestamptz not null default now(),\n"
        "  author     text not null,\n"
        "  primary key (node_id, rev),\n"
        "  unique (node_id, rev, mm_version)\n"
        ");"
    )
    statements.append(
        "CREATE TABLE rel_signature (\n"
        "  rel        text not null,\n"
        "  src_kind   node_kind not null,\n"
        "  dst_kind   node_kind not null,\n"
        "  src_min    integer not null default 0,\n"
        "  src_max    integer,\n"
        "  dst_min    integer not null default 0,\n"
        "  dst_max    integer,\n"
        "  acyclic    boolean not null default true,\n"
        "  primary key (rel, src_kind, dst_kind)\n"
        ");"
    )
    statements.append(
        "CREATE TABLE link (\n"
        "  id         bigint generated always as identity primary key,\n"
        "  rel        text not null,\n"
        "  src_id     bigint not null,\n"
        "  src_kind   node_kind not null,\n"
        "  dst_id     bigint not null,\n"
        "  dst_kind   node_kind not null,\n"
        "  conf_rev   integer not null,\n"
        "  created_at timestamptz not null default now(),\n"
        "  retired_at timestamptz,\n"
        "  foreign key (src_id, src_kind) references node(id, kind),\n"
        "  foreign key (dst_id, dst_kind) references node(id, kind),\n"
        "  foreign key (rel, src_kind, dst_kind) references rel_signature,\n"
        "  foreign key (dst_id, conf_rev) references node_revision(node_id, rev),\n"
        "  unique (rel, src_id, dst_id)\n"
        ");"
    )

    for entry in descriptor["types"]:
        statements += _create_table({"type": entry})[0]
        for field in entry["fields"]:
            if field["required"] and field["required_since"]:
                statements += _require_since({"type": entry["code"], "field": field})[0]

    for relation in descriptor["relations"]:
        statements += signature_rows(relation)

    return statements


def _table(code: str) -> str:
    return f"{code.lower()}_revision"


def _null(value: Any) -> str:
    return "null" if value is None else str(value)


_RENDERERS = {
    "add_column": _add_column,
    "drop_column": _drop_column,
    "rename_column": _rename_column,
    "alter_column_type": _alter_column_type,
    "require_since": _require_since,
    "create_table": _create_table,
    "drop_table": _drop_table,
    "add_node_kind": _add_node_kind,
    "add_enum_value": _add_enum_value,
    "insert_signature": _insert_signature,
    "delete_signature": _delete_signature,
    "update_signature": _update_signature,
}
