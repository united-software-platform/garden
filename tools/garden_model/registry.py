"""Реестр выпусков: какая версия модели какие миграции породила.

Реестр — производный указатель, а не источник истины: цепочка версий восстанавливается
из самих миграций по хешам. Его задача — быстро ответить, что уже выпущено, и заметить
правку файла, который уже выпущен.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

FILE = "registry.yaml"
HEADER = "# Реестр выпусков. Файл генерируется, править вручную не нужно.\n"


def checksum(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load(changelog_root: Path) -> list[dict[str, Any]]:
    path = changelog_root / FILE
    if not path.exists():
        return []
    return yaml.safe_load(path.read_text(encoding="utf-8")) or []


def record(changelog_root: Path, descriptor: dict[str, Any], files: list[Path]) -> None:
    """Записать выпуск версии: её хеш и контрольные суммы выпущенных файлов."""
    entries = load(changelog_root)
    entries.append(
        {
            "version": descriptor["version"],
            "hash": descriptor["hash"],
            "files": [
                {"path": str(p.relative_to(changelog_root)), "checksum": checksum(p)} for p in files
            ],
        }
    )
    (changelog_root / FILE).write_text(
        HEADER + yaml.safe_dump(entries, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def verify_immutability(changelog_root: Path) -> list[str]:
    """Проверить, что ни один выпущенный файл не изменён и не удалён."""
    problems: list[str] = []
    for entry in load(changelog_root):
        for item in entry["files"]:
            path = changelog_root / item["path"]
            if not path.exists():
                problems.append(f"{item['path']}: файл выпуска {entry['version']} удалён")
            elif checksum(path) != item["checksum"]:
                problems.append(
                    f"{item['path']}: файл выпуска {entry['version']} изменён после выпуска"
                )
    return problems
