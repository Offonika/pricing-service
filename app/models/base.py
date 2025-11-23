from typing import Any

from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


class Base(DeclarativeBase):
    """Base class for declarative models with automatic table naming."""

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    @declared_attr.directive
    def __tablename__(cls) -> str:
        return cls.__name__.lower()

    def __repr__(self) -> str:
        attrs = []
        for key in ("id",):
            value = getattr(self, key, None)
            if value is not None:
                attrs.append(f"{key}={value!r}")
        joined = " ".join(attrs)
        return f"<{self.__class__.__name__} {joined}>"
