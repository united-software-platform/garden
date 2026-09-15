"""Командная строка: сборка модели, сравнение, выпуск, проверки."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import registry
from .checks import checks_for, run_checks
from .descriptor import compile_model, dump_descriptor
from .errors import ModelError
from .gen.changelog import emit, emit_baseline
from .model import load_model
from .release import find_previous, plan_release, record_taken
from .verify import verify_schema

DEFAULT_MODEL = Path("model/garden.reqs.v1")
DEFAULT_RELEASES = Path("model/releases")
DEFAULT_CHANGELOG = Path("changelog")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    # Обработчик подставлен разбором аргументов и из Namespace приходит нетипизированным:
    # тип объявляется здесь, у единственной точки вызова.
    handler: Callable[[argparse.Namespace], int] = args.handler
    try:
        return handler(args)
    except ModelError as error:
        print(f"ОШИБКА  {error}", file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="garden-model", description="Контракт модели данных графа требований"
    )
    sub = parser.add_subparsers(required=True)

    for name, handler, help_text in (
        ("build", _build, "собрать дескриптор рабочей модели"),
        ("diff", _diff, "сравнить рабочую модель с последней выпущенной версией"),
        ("gen", _gen, "выпустить версию: миграции, дескриптор, реестр"),
        ("checks", _checks, "напечатать запросы проверок целостности"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--model", type=Path, default=DEFAULT_MODEL)
        command.add_argument("--releases", type=Path, default=DEFAULT_RELEASES)
        command.add_argument("--changelog", type=Path, default=DEFAULT_CHANGELOG)
        command.add_argument(
            "--baseline", action="store_true", help="выпустить свёртку модели целиком"
        )
        command.set_defaults(handler=handler)

    verify = sub.add_parser("verify", help="сверить схему хранилища с моделью и прогнать проверки")
    verify.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    verify.add_argument("--changelog", type=Path, default=DEFAULT_CHANGELOG)
    verify.add_argument("--dsn", required=True, help="строка подключения к хранилищу")
    verify.set_defaults(handler=_verify)
    return parser


def _descriptor(args: argparse.Namespace) -> dict[str, Any]:
    return compile_model(load_model(args.model))


def _build(args: argparse.Namespace) -> int:
    descriptor = _descriptor(args)
    print(f"{descriptor['model']} {descriptor['version']}  {descriptor['hash']}")
    return 0


def _diff(args: argparse.Namespace) -> int:
    descriptor = _descriptor(args)
    previous = find_previous(args.releases, descriptor["major"])
    print(plan_release(descriptor, previous).report())
    return 0


def _gen(args: argparse.Namespace) -> int:
    descriptor = _descriptor(args)
    previous = find_previous(args.releases, descriptor["major"])

    if args.baseline:
        emitted = emit_baseline(descriptor, args.changelog)
    else:
        plan = plan_release(descriptor, previous)
        print(plan.report())
        if plan.is_noop:
            return 0
        emitted = emit(plan, args.changelog)
        for line in record_taken(args.model, plan.changes):
            print(f"занято навсегда: {line}")
        # Перечень занятых номеров пополнен — дескриптор пересобирается, чтобы выпущенный
        # файл содержал закрытые номера. На хеш это не влияет: taken в него не входит.
        descriptor = _descriptor(args)

    args.releases.mkdir(parents=True, exist_ok=True)
    (args.releases / f"{descriptor['version']}.json").write_text(
        dump_descriptor(descriptor), encoding="utf-8"
    )
    registry.record(args.changelog, descriptor, emitted.files)
    for path in emitted.files:
        print(f"выпущено: {path}")
    return 0


def _checks(args: argparse.Namespace) -> int:
    for check in checks_for(_descriptor(args)):
        print(f"-- {check.name}: {check.subject}\n{check.sql}\n")
    return 0


def _verify(args: argparse.Namespace) -> int:
    import psycopg

    descriptor = _descriptor(args)
    problems = [f"changelog: {item}" for item in registry.verify_immutability(args.changelog)]

    with psycopg.connect(args.dsn, autocommit=True) as connection:

        def fetch(sql: str) -> list[tuple[Any, ...]]:
            return connection.execute(sql).fetchall()

        problems += [f"схема: {drift}" for drift in verify_schema(descriptor, fetch)]
        problems += [
            f"целостность: {violation}" for violation in run_checks(checks_for(descriptor), fetch)
        ]

    if not problems:
        print(f"схема соответствует модели {descriptor['version']}")
        return 0
    for problem in problems:
        print(f"РАСХОЖДЕНИЕ  {problem}", file=sys.stderr)
    return 1
