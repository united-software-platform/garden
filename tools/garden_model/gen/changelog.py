"""Раскладка, именование и порядок применения миграций.

Имя файла — метка времени: уникально без общего счётчика и не конфликтует при слиянии
веток. Порядок применения именами не задаётся: его задаёт `master.yaml` с явными
включениями, потому что хронология выпуска не совпадает с зависимостями элементов.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from ..diff import Change
from ..release import ReleasePlan
from . import postgres
from .ops import CORE, RELATIONS, Operation, operations_for

AUTHOR = "model-gen"
MASTER = "master.yaml"

#: Порядок каталогов внутри одной версии: сначала общие элементы, затем типы, затем связи.
FOLDER_RANK = {CORE: 0, RELATIONS: 2}
TYPE_RANK = 1


@dataclass
class Emitted:
    """Что выпущено для одной версии модели."""

    files: list[Path]
    warnings: list[Change]

    def paths(self, root: Path) -> list[str]:
        return [str(p.relative_to(root)) for p in self.files]


def utc_clock() -> datetime:
    return datetime.now(timezone.utc)


def emit(
    plan: ReleasePlan,
    changelog_root: Path,
    *,
    clock: Callable[[], datetime] = utc_clock,
    backend=postgres,
) -> Emitted:
    """Выпустить миграции версии и дописать порядок применения в `master.yaml`."""
    if plan.is_noop:
        # Модель не изменилась — выпускать нечего. Повторный прогон файлов не создаёт.
        return Emitted(files=[], warnings=[])

    changelog_root.mkdir(parents=True, exist_ok=True)
    descriptor = plan.current
    version = descriptor["version"]
    moment = clock()
    written: list[Path] = []

    for index, op in enumerate(_ordered(plan)):
        statements, rollback = backend.render(op)
        written.append(
            _write(changelog_root, op.folder, moment, index, _body(op.slug, version, op.comment, statements, rollback))
        )

    closing = _closing(plan, backend, version)
    written.append(_write(changelog_root, CORE, moment, len(written), closing))

    _append_master(changelog_root, written)
    return Emitted(files=written, warnings=plan.warnings)


def emit_baseline(
    descriptor: dict[str, Any],
    changelog_root: Path,
    *,
    clock: Callable[[], datetime] = utc_clock,
    backend=postgres,
) -> Emitted:
    """Выпустить свёртку модели целиком — начальную миграцию для пустого хранилища."""
    changelog_root.mkdir(parents=True, exist_ok=True)
    moment = clock()
    body = _body(
        f"baseline.{descriptor['version']}",
        descriptor["version"],
        f"начальная схема модели {descriptor['model']} {descriptor['version']}",
        backend.baseline_statements(descriptor),
        ["-- начальная схема откатывается пересозданием хранилища"],
    )
    written = [_write(changelog_root, CORE, moment, 0, body)]

    statements, rollback, _ = backend.close_version(None, descriptor["version"], descriptor["hash"])
    written.append(
        _write(
            changelog_root,
            CORE,
            moment,
            1,
            _body(f"close.{descriptor['version']}", descriptor["version"],
                  f"отпечаток модели {descriptor['version']}", statements, rollback),
        )
    )
    _append_master(changelog_root, written)
    return Emitted(files=written, warnings=[])


def _ordered(plan: ReleasePlan) -> list[Operation]:
    """Операции в топологическом порядке: общие элементы, типы узлов, связи."""
    operations: list[Operation] = []
    for change in plan.changes:
        operations += operations_for(change)
    return sorted(operations, key=lambda op: (FOLDER_RANK.get(op.folder, TYPE_RANK), op.slug))


def _closing(plan: ReleasePlan, backend, version: str) -> str:
    previous_hash = plan.previous["hash"] if plan.previous else None
    statements, rollback, precondition = backend.close_version(previous_hash, version, plan.current["hash"])
    comment = f"{plan.previous['version'] if plan.previous else 'начало'} -> {version}"
    if not plan.changes:
        comment += "; схема не меняется"
    return _body(f"close.{version}", version, comment, statements, rollback, precondition=precondition)


def _body(
    slug: str,
    version: str,
    comment: str,
    statements: Iterable[str],
    rollback: Iterable[str],
    *,
    precondition: str | None = None,
) -> str:
    lines = ["--liquibase formatted sql", ""]
    lines.append(f"--changeset {AUTHOR}:{slug} labels:model-{version}")
    lines.append(f"--comment: {comment}")
    if precondition:
        lines.append("--preconditions onFail:HALT")
        lines.append(f"--precondition-sql-check expectedResult:1 {precondition}")
    lines += list(statements)
    lines += [f"--rollback {line}" for line in rollback]
    return "\n".join(lines) + "\n"


def _write(root: Path, folder: str, moment: datetime, offset: int, body: str) -> Path:
    """Записать файл миграции. Метки времени внутри одного прогона идут подряд."""
    directory = root / folder
    directory.mkdir(parents=True, exist_ok=True)
    # Метка времени имеет точность до секунды, а соседний выпуск может начаться в ту же
    # секунду. Занятое имя не перезаписывается: это молча уничтожило бы уже выпущенную
    # миграцию — берётся следующая свободная секунда.
    stamp = int(moment.replace(microsecond=0).timestamp()) + offset
    while True:
        name = datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("Version%Y%m%d%H%M%S.sql")
        path = directory / name
        if not path.exists():
            break
        stamp += 1
    path.write_text(body, encoding="utf-8")
    return path


def _append_master(root: Path, written: list[Path]) -> None:
    """Дописать порядок применения: master перечисляет включения явно, а не сортировкой."""
    path = root / MASTER
    existing: list[dict[str, Any]] = []
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        existing = data.get("databaseChangeLog", [])
    entries = existing + [
        {"include": {"file": str(p.relative_to(root)), "relativeToChangelogFile": True}} for p in written
    ]
    header = "# Порядок применения миграций. Файл генерируется, править вручную не нужно.\n"
    path.write_text(
        header + yaml.safe_dump({"databaseChangeLog": entries}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
