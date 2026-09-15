"""Сравнение версий, классификация операций и ошибки процесса выпуска."""

import json

import pytest
import yaml
from garden_model.descriptor import compile_model
from garden_model.diff import BREAKING, DESTRUCTIVE, SAFE, diff_descriptors
from garden_model.errors import ModelError
from garden_model.model import load_model
from garden_model.release import GateError, find_previous, plan_release, record_taken


def _compile(root):
    return compile_model(load_model(root))


def _edit(root, mutate, *, version=None, file="types/br.yaml"):
    path = root / file
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(raw)
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    if version is not None:
        manifest = root / "_manifest.yaml"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        data["version"] = version
        manifest.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return _compile(root)


# --- дельта ---------------------------------------------------------------


def test_переименование_опознаётся_по_номеру(model_root, model_copy):
    before = _compile(model_root)
    after = _edit(
        model_copy,
        lambda raw: raw["fields"].__setitem__(3, {"id": 4, "name": "provenance", "type": "text"}),
        version="1.3.0",
    )

    changes = diff_descriptors(before, after)
    assert [c.op for c in changes] == ["field_renamed"]
    assert changes[0].kind == SAFE
    assert "origin -> provenance" in changes[0].detail


def test_добавление_поля_безопасно(model_root, model_copy):
    before = _compile(model_root)
    after = _edit(
        model_copy,
        lambda raw: raw["fields"].append({"id": 6, "name": "owner", "type": "text"}),
        version="1.3.0",
    )

    changes = diff_descriptors(before, after)
    assert [(c.op, c.kind) for c in changes] == [("field_added", SAFE)]


def test_удаление_поля_разрушающее_но_не_останавливает(model_root, model_copy):
    before = _compile(model_root)
    after = _edit(model_copy, lambda raw: raw["fields"].pop(3), version="1.3.0")

    plan = plan_release(after, before)
    assert [c.op for c in plan.changes] == ["field_removed"]
    assert plan.changes[0].kind == DESTRUCTIVE
    assert "данные колонки origin будут удалены" in plan.changes[0].detail
    assert plan.warnings, "разрушающая операция попадает в предупреждения"


def test_обязательное_поле_без_required_since_ломает_контракт(model_root, model_copy):
    before = _compile(model_root)
    after = _edit(
        model_copy,
        lambda raw: raw["fields"].append(
            {"id": 6, "name": "owner", "type": "text", "required": True}
        ),
        version="1.3.0",
    )

    changes = diff_descriptors(before, after)
    assert changes[0].kind == BREAKING


def test_обязательное_поле_с_required_since_безопасно(model_root, model_copy):
    before = _compile(model_root)
    after = _edit(
        model_copy,
        lambda raw: raw["fields"].append(
            {"id": 6, "name": "owner", "type": "text", "required": True, "required_since": 3}
        ),
        version="1.3.0",
    )

    assert diff_descriptors(before, after)[0].kind == SAFE


def test_сужение_кодомена_связи_ломает_контракт(model_root, model_copy):
    before = _compile(model_root)
    _edit(
        model_copy,
        lambda raw: raw["relations"][0].__setitem__("to", ["BR", "FR"]),
        file="relations.yaml",
    )
    widened = _compile(model_copy)
    assert diff_descriptors(before, widened)[0].kind == SAFE

    narrowed = diff_descriptors(widened, before)
    assert narrowed[0].op == "relation_to_narrowed"
    assert narrowed[0].kind == BREAKING


def test_ужесточение_кардинальности_ломает_контракт(model_root, model_copy):
    before = _compile(model_root)
    after = _edit(
        model_copy,
        lambda raw: raw["relations"][0].__setitem__("to_cardinality", {"min": 1}),
        file="relations.yaml",
    )

    change = diff_descriptors(before, after)[0]
    assert change.op == "relation_cardinality"
    assert change.kind == BREAKING
    assert "ужесточена" in change.detail


def test_удаление_значения_перечисления_ломает_контракт(model_root, model_copy):
    before = _compile(model_root)
    after = _edit(
        model_copy, lambda raw: raw["enums"]["req_status"]["values"].pop(), file="_manifest.yaml"
    )

    change = diff_descriptors(before, after)[0]
    assert (change.op, change.kind) == ("enum_value_removed", BREAKING)


# --- ошибки процесса ------------------------------------------------------


def test_модель_изменена_без_повышения_версии(model_root, model_copy):
    before = _compile(model_root)
    after = _edit(
        model_copy, lambda raw: raw["fields"].append({"id": 6, "name": "owner", "type": "text"})
    )

    with pytest.raises(GateError, match="повысьте средний разряд"):
        plan_release(after, before)


def test_версия_понижена(model_root, model_copy):
    before = {**_compile(model_root), "version": "1.5.0"}
    after = _edit(
        model_copy,
        lambda raw: raw["fields"].append({"id": 6, "name": "owner", "type": "text"}),
        version="1.3.0",
    )

    with pytest.raises(GateError, match="версия понижена"):
        plan_release(after, before)


def test_версия_повышена_без_изменений(model_root, model_copy):
    before = _compile(model_root)
    after = _edit(model_copy, lambda raw: None, version="1.3.0")

    with pytest.raises(GateError, match="структура модели не изменилась"):
        plan_release(after, before)


def test_модель_не_изменялась_выпускать_нечего(model_root):
    current = _compile(model_root)

    plan = plan_release(current, current)
    assert plan.is_noop
    assert "выпускать нечего" in plan.report()


def test_повторная_выдача_номера_поля(model_root, model_copy):
    before = _compile(model_root)

    def mutate(raw):
        raw["fields"] = [f for f in raw["fields"] if f["id"] != 4]
        raw["fields"].append({"id": 4, "name": "owner", "type": "text"})

    after = _edit(model_copy, mutate, version="1.3.0")
    # дельта показала бы переименование, но номер 4 остаётся тем же полем —
    # повторная выдача ловится только после того, как поле было удалено.
    plan = plan_release(after, before)
    assert [c.op for c in plan.changes] == ["field_renamed"]


def test_номер_удалённого_поля_повторно_не_выдаётся(model_root, model_copy):
    before = _compile(model_root)
    removed = _edit(model_copy, lambda raw: raw["fields"].pop(3), version="1.3.0")
    plan = plan_release(removed, before)
    record_taken(model_copy, plan.changes)

    with pytest.raises(ModelError, match="выдан повторно"):
        _edit(
            model_copy,
            lambda raw: raw["fields"].append({"id": 4, "name": "owner", "type": "text"}),
            version="1.4.0",
        )


def test_занятые_номера_дописываются_инструментом(model_root, model_copy):
    before = _compile(model_root)
    after = _edit(model_copy, lambda raw: raw["fields"].pop(3), version="1.3.0")
    plan = plan_release(after, before)

    recorded = record_taken(model_copy, plan.changes)
    assert recorded == ["BR.4 (origin)"]

    br = load_model(model_copy).type_by_code("BR")
    assert br is not None
    assert 4 in br.taken.fields and "origin" in br.taken.names


# --- поиск выпущенной версии ---------------------------------------------


def test_находится_последняя_выпущенная_версия(tmp_path, model_root):
    releases = tmp_path / "releases"
    releases.mkdir()
    current = _compile(model_root)
    for version in ("1.1.0", "1.2.0", "2.0.0"):
        (releases / f"{version}.json").write_text(
            json.dumps({**current, "version": version}), encoding="utf-8"
        )

    previous_of_first, previous_of_second = (
        find_previous(releases, major=1),
        find_previous(releases, major=2),
    )
    assert previous_of_first is not None and previous_of_first["version"] == "1.2.0"
    assert previous_of_second is not None and previous_of_second["version"] == "2.0.0"
    assert find_previous(releases, major=3) is None


def test_пополнение_занятых_номеров_не_меняет_хеш(model_root, model_copy):
    """Иначе хеш модели навсегда разошёлся бы с хешем, записанным в хранилище."""
    before = _compile(model_root)
    after = _edit(model_copy, lambda raw: raw["fields"].pop(3), version="1.3.0")
    plan = plan_release(after, before)

    record_taken(model_copy, plan.changes)

    assert _compile(model_copy)["hash"] == after["hash"]
