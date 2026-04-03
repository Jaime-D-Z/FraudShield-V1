# ============================================================
#  audit-service  —  main.py
#  Consume transaction.results y persiste trail inmutable
# ============================================================
import asyncio
import os
from datetime import datetime, timezone
import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator
from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy import Column, String, Numeric, DateTime, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase
import uuid

try:
    from .database import AsyncSessionLocal, engine
except ImportError:  # pragma: no cover - fallback para ejecucion local directa
    from database import AsyncSessionLocal, engine

log = structlog.get_logger()
tracer = trace.get_tracer("audit-service")

app = FastAPI(title="FraudShield — Audit Service")
Instrumentator().instrument(app).expose(app)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
STREAM_NAME = "transaction.results"
GROUP_NAME = "audit-group"


# ─── MODELO (append-only, nunca se modifica) ─────────────────
class Base(DeclarativeBase):
    pass


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    decision = Column(String, nullable=False)
    risk_score = Column(Numeric(5, 2), nullable=True)
    reasons = Column(Text, nullable=True)
    recorded_at = Column(DateTime(timezone=True), nullable=False)


@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    asyncio.create_task(consume_stream())
    log.info("audit-service started")


async def consume_stream():
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        await redis.xgroup_create(STREAM_NAME, GROUP_NAME, id="0", mkstream=True)
    except Exception:
        pass

    while True:
        try:
            messages = await redis.xreadgroup(
                groupname=GROUP_NAME,
                consumername="audit-1",
                streams={STREAM_NAME: ">"},
                count=10,
                block=2000,
            )
            for stream, entries in messages or []:
                for msg_id, fields in entries:
                    async with AsyncSessionLocal() as db:
                        entry = AuditLog(
                            transaction_id=fields["transaction_id"],
                            user_id=fields["user_id"],
                            decision=fields["decision"],
                            risk_score=float(fields.get("score", 0)),
                            reasons=fields.get("reasons"),
                            recorded_at=datetime.now(timezone.utc),
                        )
                        db.add(entry)
                        await db.commit()
                        log.info(
                            "audit_recorded",
                            txn=fields["transaction_id"],
                            decision=fields["decision"],
                        )
                    await redis.xack(STREAM_NAME, GROUP_NAME, msg_id)
        except Exception as e:
            log.error("audit_consumer_error", error=str(e))
            await asyncio.sleep(2)


@app.get("/audit/{transaction_id}")
async def get_audit(transaction_id: str, skip: int = 0, limit: int = 50):
    """
    TODO para Copilot/Claude:
    'Implementa get_audit: busca todos los AuditLog de ese
     transaction_id ordenados por recorded_at asc. Retorna lista.'
    """
    with tracer.start_as_current_span("get_audit") as span:
        span.set_attribute("transaction_id", transaction_id)
        span.set_attribute("skip", skip)
        span.set_attribute("limit", limit)

        async with AsyncSessionLocal() as db:
            stmt = (
                select(AuditLog)
                .where(AuditLog.transaction_id == transaction_id)
                .order_by(AuditLog.recorded_at.asc())
                .offset(skip)
                .limit(limit)
            )
            result = await db.execute(stmt)
            rows = result.scalars().all()

        data = [
            {
                "id": str(row.id),
                "transaction_id": row.transaction_id,
                "user_id": row.user_id,
                "decision": row.decision,
                "risk_score": (
                    float(row.risk_score) if row.risk_score is not None else None
                ),
                "reasons": row.reasons,
                "recorded_at": row.recorded_at.isoformat() if row.recorded_at else None,
            }
            for row in rows
        ]
        log.info(
            "audit_fetched",
            transaction_id=transaction_id,
            count=len(data),
            skip=skip,
            limit=limit,
        )
        return data


@app.get("/health")
async def health():
    return {"status": "ok", "service": "audit-service"}
