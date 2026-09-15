"""Общие приспособления тестов."""

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def model_root() -> Path:
    """Каталог образцовой модели `garden.reqs.v1`."""
    return FIXTURES / "model" / "garden.reqs.v1"


@pytest.fixture
def model_copy(model_root: Path, tmp_path: Path) -> Path:
    """Копия образцовой модели, которую тест может править."""
    import shutil

    target = tmp_path / "garden.reqs.v1"
    shutil.copytree(model_root, target)
    return target


@pytest.fixture
def trade_model() -> Path:
    """Каталог демонстрационной модели предметной области «торговля»."""
    return FIXTURES / "model" / "trade.v1"


@pytest.fixture
def trade_copy(trade_model: Path, tmp_path: Path) -> Path:
    """Копия демонстрационной модели, которую тест проводит через выпуски версий."""
    import shutil

    target = tmp_path / "trade.v1"
    shutil.copytree(trade_model, target)
    return target


def statements_of(path: Path) -> list[str]:
    """Операторы SQL из файла миграции: служебные строки Liquibase отбрасываются."""
    body = [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("--")
    ]
    return [chunk + ";" for chunk in "\n".join(body).split(";") if chunk.strip()]
