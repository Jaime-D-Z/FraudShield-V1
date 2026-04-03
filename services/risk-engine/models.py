from datetime import datetime
from enum import Enum
import uuid

from sqlalchemy import Column, DateTime, Enum as SAEnum, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase
from pydantic import BaseModel


class Base(DeclarativeBase):
    pass


class TransactionStatus(str, Enum):
    PENDING = "PENDING"
    ANALYZING = "ANALYZING"
    APPROVED = "APPROVED"
    HELD = "HELD"
    BLOCKED = "BLOCKED"


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String, nullable=False, index=True)
    amount = Column(Numeric(18, 2), nullable=False)
    currency = Column(String(3), nullable=False)
    merchant_id = Column(String, nullable=False)
    merchant_country = Column(String(2), nullable=False)
    status = Column(SAEnum(TransactionStatus), nullable=False)
    risk_score = Column(Numeric(5, 2), nullable=True)
    fraud_reasons = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=True)
    idempotency_key = Column(String, unique=True, nullable=False)


class RiskResult(BaseModel):
    transaction_id: str
    decision: TransactionStatus
    score: int
    reasons: list[str]
    analyzed_at: datetime
