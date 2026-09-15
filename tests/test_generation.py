"""Генерация миграций: раскладка, именование, порядок, замыкающая миграция, свёртка."""

from datetime import datetime, timezone

import yaml

from garden_model.descriptor import compile_model
from garden_model.gen.changelog import emit, emit_baseline
from garden_model.model import load_model
from garden_model.release import plan_release

MOMENT = datetime(2026, 9, 15, 14, 22, 33, tzinfo=timezone.utc)


def _clock():
    return MOMENT


def _compile(root):
    return compile_model(load_model(root))


def _edit(root, mutate, version, file="types/br.yaml"):
    path = root / file
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(raw)
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    manifest = root / "_manifest.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["version"] = version
    manifest.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return _compile(root)


def _emit(model_root, model_copy, mutate, version="1.3.0", file="types/br.yaml", out=None):
    before = _compile(model_root)
    after = _edit(model_copy, mutate, version, file)
    plan = plan_release(after, before)
    return plan, emit(plan, out, clock=_clock)


def test_добавление_поля_кладётся_в_каталог_типа(model_root, model_copy, tmp_path):
    out = tmp_path / "changelog"
    _, emitted = _emit(model_root, model_copy, lambda raw: raw["fields"].append({"id": 6, "name": "owner", "type": "text"}), out=out)

    paths = emitted.paths(out)
    assert "br/Version20260915142233.sql" in paths
    body = (out / "br" / "Version20260915142233.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE br_revision ADD COLUMN owner text;" in body
    assert "--rollback ALTER TABLE br_revision DROP COLUMN owner;" in body
    assert "--changeset model-gen:BR.f6.add labels:model-1.3.0" in body


def test_переименование_выпускает_rename(model_root, model_copy, tmp_path):
    out = tmp_path / "changelog"
    _, emitted = _emit(
        model_root, model_copy,
        lambda raw: raw["fields"].__setitem__(3, {"id": 4, "name": "provenance", "type": "text"}),
        out=out,
    )
    body = (out / "br" / "Version20260915142233.sql").read_text(encoding="utf-8")
    assert "RENAME COLUMN origin TO provenance;" in body
    assert "--rollback ALTER TABLE br_revision RENAME COLUMN provenance TO origin;" in body


def test_удаление_поля_выпускается_с_предупреждением(model_root, model_copy, tmp_path):
    out = tmp_path / "changelog"
    plan, emitted = _emit(model_root, model_copy, lambda raw: raw["fields"].pop(3), out=out)

    body = (out / "br" / "Version20260915142233.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE br_revision DROP COLUMN origin;" in body
    assert emitted.warnings, "выпуск состоялся, но предупреждение выдано"
    assert "данные колонки origin будут удалены" in plan.report()


def test_замыкающая_миграция_несёт_хеш_и_предусловие(model_root, model_copy, tmp_path):
    out = tmp_path / "changelog"
    plan, emitted = _emit(model_root, model_copy, lambda raw: raw["fields"].append({"id": 6, "name": "owner", "type": "text"}), out=out)

    closing = emitted.files[-1].read_text(encoding="utf-8")
    assert "--preconditions onFail:HALT" in closing
    assert plan.previous["hash"] in closing, "предусловие сверяет прежний хеш"
    assert f"UPDATE model_state SET version = '1.3.0', hash = '{plan.current['hash']}';" in closing


def test_версия_без_изменений_схемы_выпускает_замыкающую(model_root, model_copy, tmp_path):
    out = tmp_path / "changelog"
    before = _compile(model_root)

    def mutate(raw):
        raw["fields"] = [f for f in raw["fields"] if f["id"] != 4]
        raw.setdefault("taken", {}).setdefault("fields", []).append(4)

    after = _edit(model_copy, mutate, "1.3.0")
    plan = plan_release(after, before)
    emitted = emit(plan, out, clock=_clock)

    assert len(emitted.files) == 2, "удаление колонки и замыкающая"
    assert "схема не меняется" not in emitted.files[-1].read_text(encoding="utf-8")


def test_порядок_в_master_топологический(model_root, model_copy, tmp_path):
    out = tmp_path / "changelog"

    def mutate(raw):
        raw["fields"].append({"id": 6, "name": "owner", "type": "text"})

    before = _compile(model_root)
    path = model_copy / "types" / "tc.yaml"
    path.write_text(
        yaml.safe_dump(
            {"type": "TC", "id": 3, "name": {"ru": "Тест-кейс", "en": "Test Case"},
             "fields": [{"id": 1, "name": "title", "type": "text", "required": True}]},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    rel = model_copy / "relations.yaml"
    raw = yaml.safe_load(rel.read_text(encoding="utf-8"))
    raw["relations"].append({"id": 2, "name": "verified_by", "from": ["FR"], "to": ["TC"]})
    rel.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    after = _edit(model_copy, mutate, "1.3.0")

    emitted = emit(plan_release(after, before), out, clock=_clock)
    order = [e["include"]["file"] for e in yaml.safe_load((out / "master.yaml").read_text(encoding="utf-8"))["databaseChangeLog"]]

    kind_index = next(i for i, f in enumerate(order) if "kind.TC" in (out / f).read_text(encoding="utf-8"))
    table_index = next(i for i, f in enumerate(order) if f.startswith("tc/"))
    rel_index = next(i for i, f in enumerate(order) if f.startswith("rel/"))
    assert kind_index < table_index < rel_index, "общие элементы, затем типы, затем связи"
    assert order[-1].startswith("core/"), "замыкающая миграция последняя"


def test_свёртка_модели_создаёт_всю_схему(model_root, tmp_path):
    out = tmp_path / "changelog"
    descriptor = _compile(model_root)
    emitted = emit_baseline(descriptor, out, clock=_clock)

    body = emitted.files[0].read_text(encoding="utf-8")
    for fragment in (
        "CREATE TYPE req_status AS ENUM ('DRAFT', 'APPROVED', 'RETIRED');",
        "CREATE TYPE node_kind AS ENUM ('BR', 'FR');",
        "CREATE TABLE node (",
        "CREATE TABLE br_revision (",
        "CREATE TABLE rel_signature (",
        "INSERT INTO rel_signature (rel, src_kind, dst_kind",
    ):
        assert fragment in body, fragment
    assert "CHECK (mm_version < 2 OR rationale IS NOT NULL) NOT VALID" in body
    assert descriptor["hash"] in emitted.files[-1].read_text(encoding="utf-8")


def test_метки_времени_в_одном_прогоне_не_совпадают(model_root, model_copy, tmp_path):
    out = tmp_path / "changelog"

    def mutate(raw):
        raw["fields"].append({"id": 6, "name": "owner", "type": "text"})
        raw["fields"].append({"id": 8, "name": "note", "type": "text"})

    before = _compile(model_root)
    after = _edit(model_copy, mutate, "1.3.0")
    emitted = emit(plan_release(after, before), out, clock=_clock)

    names = [p.name for p in emitted.files]
    assert len(names) == len(set(names)), "имена уникальны"


def test_повторный_прогон_не_создаёт_файлов(model_root, tmp_path):
    out = tmp_path / "changelog"
    descriptor = _compile(model_root)

    emitted = emit(plan_release(descriptor, descriptor), out, clock=_clock)
    assert emitted.files == []
    assert not out.exists() or not list(out.rglob("*.sql"))


def test_соседний_выпуск_не_перезаписывает_файл(model_root, model_copy, tmp_path):
    """Два выпуска подряд могут прийтись на одну секунду: занятое имя не перезаписывается."""
    out = tmp_path / "changelog"
    first = _compile(model_root)
    second = _edit(model_copy, lambda raw: raw["fields"].append({"id": 6, "name": "owner", "type": "text"}), "1.3.0")
    emitted_first = emit(plan_release(second, first), out, clock=_clock)

    third = _edit(model_copy, lambda raw: raw["fields"].append({"id": 8, "name": "note", "type": "text"}), "1.4.0")
    emitted_second = emit(plan_release(third, second), out, clock=_clock)

    paths = [p.resolve() for p in emitted_first.files + emitted_second.files]
    assert len(paths) == len(set(paths)), "имена не пересекаются между выпусками"
    for path in emitted_first.files:
        assert f"model-1.3.0" in path.read_text(encoding="utf-8"), "первый выпуск не перезаписан"
