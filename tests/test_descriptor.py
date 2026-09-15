"""Компиляция модели в дескриптор: раскрытие умолчаний, детерминированность, хеш."""

import pytest
import yaml

from garden_model.descriptor import compile_model, dump_descriptor
from garden_model.errors import ModelError
from garden_model.model import load_model


def _compile(root):
    return compile_model(load_model(root))


def test_дескриптор_самодостаточен(model_root):
    d = _compile(model_root)

    assert d["model"] == "garden.reqs"
    assert d["version"] == "1.2.0"
    assert (d["major"], d["minor"], d["patch"]) == (1, 2, 0)

    br = next(t for t in d["types"] if t["code"] == "BR")
    assert br["table"] == "br_revision"
    assert br["code_pattern"] == "BR-{NNN}"

    status = next(f for f in br["fields"] if f["name"] == "status")
    assert status["values"] == ["DRAFT", "APPROVED", "RETIRED"], "перечисление раскрыто на месте"


def test_умолчания_раскрыты(model_root):
    br = next(t for t in _compile(model_root)["types"] if t["code"] == "BR")

    origin = next(f for f in br["fields"] if f["name"] == "origin")
    assert origin["required"] is False
    assert origin["required_since"] is None
    assert origin["column"] == "origin"

    title = next(f for f in br["fields"] if f["name"] == "title")
    assert title["required_since"] == 0, "обязательное без признака — обязательно с начала"

    rationale = next(f for f in br["fields"] if f["name"] == "rationale")
    assert rationale["required_since"] == 2


def test_повторная_компиляция_даёт_тот_же_файл(model_root):
    assert dump_descriptor(_compile(model_root)) == dump_descriptor(_compile(model_root))


def test_перестановка_полей_не_меняет_дескриптор(model_copy, model_root):
    path = model_copy / "types" / "br.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["fields"].reverse()
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    assert dump_descriptor(_compile(model_copy)) == dump_descriptor(_compile(model_root))


def test_правка_описания_не_меняет_хеш(model_copy, model_root):
    path = model_copy / "types" / "br.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["description"] = "совсем другое пояснение"
    raw["fields"][0]["description"] = "и здесь тоже"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    changed, original = _compile(model_copy), _compile(model_root)
    assert changed["hash"] == original["hash"]
    assert changed["types"][0]["description"] != original["types"][0]["description"]


def test_правка_структуры_меняет_хеш(model_copy, model_root):
    path = model_copy / "types" / "br.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["fields"].append({"id": 6, "name": "owner", "type": "text"})
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    assert _compile(model_copy)["hash"] != _compile(model_root)["hash"]


def test_смена_разряда_patch_не_меняет_хеш(model_copy, model_root):
    path = model_copy / "_manifest.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["version"] = "1.2.7"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    assert _compile(model_copy)["hash"] == _compile(model_root)["hash"]


def test_номер_версии_в_хеш_не_входит(model_copy, model_root):
    """Хеш описывает содержание модели. Повышение версии само по себе содержания не меняет,
    иначе случай «версию повысили, модель не тронули» нельзя было бы отличить от правки."""
    path = model_copy / "_manifest.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["version"] = "1.3.0"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    changed, original = _compile(model_copy), _compile(model_root)
    assert changed["hash"] == original["hash"]
    assert changed["version"] != original["version"]


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda raw: raw["fields"].append({"id": 1, "name": "other", "type": "text"}), "номер поля"),
        (lambda raw: raw["fields"].append({"id": 9, "name": "title", "type": "text"}), "имя поля"),
        (lambda raw: raw["fields"].append({"id": 7, "name": "again", "type": "text"}), "занятым"),
        (lambda raw: raw["fields"].append({"id": 8, "name": "note", "type": "enum.missing"}), "перечисление"),
        (lambda raw: raw["fields"].append({"id": 8, "name": "note", "type": "ref.XX"}), "тип узла"),
    ],
)
def test_валидация_ловит_нарушения(model_copy, mutate, expected):
    path = model_copy / "types" / "br.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(raw)
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ModelError, match=expected):
        _compile(model_copy)


def test_связь_на_несуществующий_тип_отвергается(model_copy):
    path = model_copy / "relations.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["relations"][0]["to"] = ["ZZ"]
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ModelError, match="несуществующий тип узла"):
        _compile(model_copy)
