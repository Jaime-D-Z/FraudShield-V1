# ============================================================
#  transaction-service  —  models.py
# ============================================================
from sqlalchemy import Column, String, Numeric, DateTime, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase
from pydantic import BaseModel, field_validator
from decimal import Decimal
from enum import Enum
import uuid


class Base(DeclarativeBase):
    pass


# ─── ENUM DE ESTADO ─────────────────────────────────────────
class TransactionStatus(str, Enum):
    PENDING = "PENDING"
    ANALYZING = "ANALYZING"
    APPROVED = "APPROVED"
    HELD = "HELD"
    BLOCKED = "BLOCKED"


# ─── MODELO ORM ─────────────────────────────────────────────
class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String, nullable=False, index=True)
    amount = Column(Numeric(18, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="USD")
    merchant_id = Column(String, nullable=False)
    merchant_country = Column(String(2), nullable=False)  # ISO 3166
    status = Column(
        SAEnum(TransactionStatus), nullable=False, default=TransactionStatus.PENDING
    )
    risk_score = Column(Numeric(5, 2), nullable=True)
    fraud_reasons = Column(String, nullable=True)  # JSON string
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=True)
    idempotency_key = Column(String, unique=True, nullable=False)


# ─── SCHEMAS PYDANTIC ────────────────────────────────────────
class TransactionCreate(BaseModel):
    amount: Decimal
    currency: str = "USD"
    merchant_id: str
    merchant_country: str
    idempotency_key: str

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v):
        if v <= 0:
            raise ValueError("amount must be positive")
        return v

    @field_validator("merchant_country")
    @classmethod
    def country_must_be_iso(cls, v):
        if len(v) != 2:
            raise ValueError("merchant_country must be ISO 3166 2-letter code")
        return v.upper()

    @field_validator("currency")
    @classmethod
    def currency_must_be_iso(cls, v):
        if len(v) != 3:
            raise ValueError("currency must be ISO 4217 3-letter code")
        return v.upper()


class TransactionResponse(BaseModel):
    transaction_id: str
    status: TransactionStatus
    risk_score: float | None = None
    fraud_reasons: list[str] | None = None

    class Config:
        from_attributes = True
