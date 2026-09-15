"""Выпуск версии: сверка с последней выпущенной, ошибки процесса, ведение занятых номеров."""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

import yaml

from .diff import BREAKING, DESTRUCTIVE, SAFE, Change, diff_descriptors
from .errors import ModelError
from .model import Version


class GateError(ModelError):
    """Ошибка процесса выпуска: её нельзя «принять решением», её надо исправить."""


@dataclass
class ReleasePlan:
    """Результат сверки рабочей модели с последней выпущенной версией."""

    previous: dict[str, Any] | None
    current: dict[str, Any]
    changes: list[Change] = dc_field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return self.previous is not None and self.previous["hash"] == self.current["hash"]

    @property
    def warnings(self) -> list[Change]:
        return [c for c in self.changes if c.kind in (BREAKING, DESTRUCTIVE)]

    def report(self) -> str:
        if self.is_noop:
            return f"модель не изменялась: {self.current['version']}, выпускать нечего"
        lines = [f"{self.previous['version'] if self.previous else 'пусто'} -> {self.current['version']}"]
        lines += [f"  {change}" for change in self.changes] or ["  начальный выпуск модели"]
        for change in self.warnings:
            lines.append(f"ВНИМАНИЕ  {change.element}: {change.detail}")
        return "\n".join(lines)


def find_previous(releases_dir: Path, major: int) -> dict[str, Any] | None:
    """Последний выпущенный дескриптор того же старшего разряда."""
    if not releases_dir.is_dir():
        return None
    best: tuple[Version, dict[str, Any]] | None = None
    for path in sorted(releases_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        version = Version.parse(data["version"], where=str(path))
        if version.major != major:
            continue
        if best is None or version > best[0]:
            best = (version, data)
    return None if best is None else best[1]


def plan_release(current: dict[str, Any], previous: dict[str, Any] | None) -> ReleasePlan:
    """Сверить рабочую модель с выпущенной и решить, можно ли выпускать."""
    if previous is None:
        return ReleasePlan(previous=None, current=current, changes=[])

    old_version = Version.parse(previous["version"], where="выпущенная версия")
    new_version = Version.parse(current["version"], where="рабочая модель")
    same_hash = previous["hash"] == current["hash"]

    if new_version < old_version:
        raise GateError(
            f"версия понижена: {new_version} ниже выпущенной {old_version}",
            where="выпуск",
        )
    if same_hash and new_version == old_version:
        return ReleasePlan(previous=previous, current=current, changes=[])
    if not same_hash and new_version == old_version:
        raise GateError(
            f"модель изменена, но версия осталась {new_version}: повысьте средний разряд",
            where="выпуск",
        )
    if same_hash and new_version > old_version:
        raise GateError(
            f"версия повышена до {new_version}, но структура модели не изменилась: выпускать нечего",
            where="выпуск",
        )

    changes = diff_descriptors(previous, current)
    _check_reuse(previous, changes)
    return ReleasePlan(previous=previous, current=current, changes=changes)


def _check_reuse(previous: dict[str, Any], changes: list[Change]) -> None:
    """Запретить повторную выдачу номера поля, который уже был занят."""
    occupied: dict[str, set[int]] = {}
    for entry in previous.get("types", []):
        occupied[entry["code"]] = {f["id"] for f in entry["fields"]} | set(entry["taken"]["fields"])

    for change in changes:
        if change.op != "field_added":
            continue
        code = change.payload["type"]
        number = change.payload["field"]["id"]
        if number in occupied.get(code, set()):
            raise GateError(
                f"номер поля {number} уже был занят в {code} и не может быть выдан повторно",
                where="выпуск",
            )


def record_taken(model_root: Path, changes: list[Change]) -> list[str]:
    """Дописать занятые номера и имена удалённых элементов в файл, который ведёт инструмент."""
    path = model_root / "_taken.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    data = data or {}
    types = data.setdefault("types", {})
    recorded: list[str] = []

    for change in changes:
        if change.op != "field_removed":
            continue
        code = change.payload["type"]
        field = change.payload["field"]
        entry = types.setdefault(code, {})
        numbers = sorted(set(entry.get("fields", [])) | {field["id"]})
        names = sorted(set(entry.get("names", [])) | {field["name"]})
        entry["fields"], entry["names"] = numbers, names
        recorded.append(f"{code}.{field['id']} ({field['name']})")

    if recorded:
        header = "# Занятые навсегда номера и имена. Файл ведёт инструмент, править вручную не нужно.\n"
        path.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8")
    return recorded
