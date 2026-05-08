from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import expression

from app.models.base import Base


class CompetitorItemMatchStatus(enum.StrEnum):
    SUGGESTED = "suggested"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"
    AMBIGUOUS = "ambiguous"


class CompetitorItemMatchMethod(enum.StrEnum):
    EMBEDDING_AUTO = "embedding_auto"
    LLM_ARBITRATE = "llm_arbitrate"
    MANUAL = "manual"


class CompetitorItemMatch(Base):
    """
    Итоговое сопоставление competitor_item → product (1:1).
    """

    __tablename__ = "competitor_item_match"
    __table_args__ = (UniqueConstraint("competitor_item_id", name="uq_comp_item_match_single"),)

    competitor_item_id: Mapped[int] = mapped_column(
        ForeignKey("competitor_item.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("product.id", ondelete="CASCADE"), nullable=False, index=True
    )

    status: Mapped[CompetitorItemMatchStatus] = mapped_column(
        Enum(
            CompetitorItemMatchStatus,
            name="competitor_item_match_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        server_default=expression.text("'suggested'"),
    )
    method: Mapped[CompetitorItemMatchMethod] = mapped_column(
        Enum(
            CompetitorItemMatchMethod,
            name="competitor_item_match_method",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        server_default=expression.text("'embedding_auto'"),
    )

    score_embed_best: Mapped[Optional[float]] = mapped_column(Numeric(6, 4))
    score_embed_gap: Mapped[Optional[float]] = mapped_column(Numeric(6, 4))
    score_llm: Mapped[Optional[float]] = mapped_column(Numeric(6, 4))
    final_score: Mapped[Optional[float]] = mapped_column(Numeric(6, 4))
    llm_confidence: Mapped[Optional[float]] = mapped_column(Numeric(4, 3))

    rationale_json: Mapped[Optional[dict]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=True
    )
    embed_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    embed_dim: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    topk_used: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    competitor_item = relationship("CompetitorItem", back_populates="match", uselist=False)
    product = relationship("Product")
