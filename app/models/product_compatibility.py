from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ProductCompatibility(Base):
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), index=True, nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="onec")

    product = relationship("Product", back_populates="compatibilities")

    __table_args__ = (
        UniqueConstraint("product_id", "value", name="uq_product_compatibility_product_value"),
    )
