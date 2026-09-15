"""Формат модели: разбор типов узлов, сигнатур связей, занятых номеров."""

import pytest
import yaml
from garden_model.errors import ModelError
from garden_model.model import Version, load_model


def test_тип_узла_разбирается_со_всеми_элементами_формата(model_root):
    model = load_model(model_root)

    assert model.package == "garden.reqs"
    assert model.version == Version(1, 2, 0)

    br = model.type_by_code("BR")
    assert br is not None
    assert br.id == 1
    assert br.name_ru == "Бизнес-требование"
    assert br.name_en == "Business Requirement"
    assert br.table == "br_revision"

    numbers = [f.id for f in br.fields]
    assert numbers == sorted(numbers), "поля упорядочены по номеру"

    title = br.fields[0]
    assert (title.id, title.name, title.required) == (1, "title", True)
    assert str(title.type) == "text"

    status = next(f for f in br.fields if f.name == "status")
    assert (status.type.kind, status.type.name) == ("enum", "req_status")


def test_перечисление_разбирается_из_манифеста(model_root):
    model = load_model(model_root)

    status = model.enum_by_name("req_status")
    assert status is not None
    assert [v.name for v in status.values] == ["DRAFT", "APPROVED", "RETIRED"]


def test_сигнатура_связи_разбирается(model_root):
    model = load_model(model_root)

    realizes = model.relations[0]
    assert realizes.name == "realizes"
    assert realizes.sources == ("FR",)
    assert realizes.targets == ("BR",)
    assert realizes.source_cardinality.min == 1
    assert realizes.source_cardinality.max is None
    assert realizes.acyclic is True


@pytest.mark.parametrize("missing", ["from", "to"])
def test_связь_без_домена_или_кодомена_отвергается(model_copy, missing):
    path = model_copy / "relations.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["relations"][0][missing] = []
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ModelError) as exc:
        load_model(model_copy)
    assert "связи" in str(exc.value)


def test_занятые_номера_имена_и_коды_разбираются(model_root):
    br = load_model(model_root).type_by_code("BR")

    assert br is not None
    assert br.taken.fields == (7,)
    assert br.taken.names == ("priority",)
    assert br.taken.codes == (19,)


def test_обязательность_начиная_с_версии_разбирается(model_root):
    br = load_model(model_root).type_by_code("BR")

    assert br is not None
    rationale = next(f for f in br.fields if f.name == "rationale")
    assert rationale.required is True
    assert rationale.required_since == 2

    origin = next(f for f in br.fields if f.name == "origin")
    assert origin.required is False
    assert origin.required_since is None


def test_required_since_без_обязательности_отвергается(model_copy):
    path = model_copy / "types" / "br.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["fields"].append({"id": 6, "name": "note", "type": "text", "required_since": 2})
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ModelError, match="required_since"):
        load_model(model_copy)


def test_неизвестный_тип_поля_отвергается(model_copy):
    path = model_copy / "types" / "br.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["fields"][0]["type"] = "money"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ModelError, match="неизвестный тип"):
        load_model(model_copy)


def test_сообщение_об_ошибке_называет_место(model_copy):
    (model_copy / "types" / "br.yaml").write_text("type: BR\nid: 1\n", encoding="utf-8")

    with pytest.raises(ModelError) as exc:
        load_model(model_copy)
    assert "types/br.yaml" in str(exc.value)
    assert "name" in str(exc.value)
