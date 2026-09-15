"""Ошибки работы с моделью."""


class ModelError(Exception):
    """Нарушение формата или целостности модели.

    Сообщение всегда называет элемент модели, в котором обнаружено нарушение,
    чтобы автор правки видел причину без чтения исходников.
    """

    def __init__(self, message: str, *, where: str | None = None) -> None:
        self.where = where
        super().__init__(f"{where}: {message}" if where else message)
