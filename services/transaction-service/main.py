# ============================================================
#  transaction-service  —  main.py
#  FastAPI · async · OpenTelemetry · Redis Streams
# ============================================================
from fastapi import FastAPI, Depends, HTTPException, Header
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_fastapi_instrumentator import Instrumentator
import os
import uuid

import redis.asyncio as aioredis
import structlog
from datetime import datetime, timezone

try:
    from .models import (
        Transaction,
        TransactionStatus,
        TransactionCreate,
        TransactionResponse,
    )
    from .database import get_db, engine, Base
except ImportError:  # pragma: no cover - fallback para ejecucion local directa
    from models import (
        Transaction,
        TransactionStatus,
        TransactionCreate,
        TransactionResponse,
    )
    from database import get_db, engine, Base

# ─── SETUP OBSERVABILIDAD ───────────────────────────────────
provider = TracerProvider()
provider.add_span_processor(
    BatchSpanProcessor(
        OTLPSpanExporter(
            endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4317")
        )
    )
)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("transaction-service")

log = structlog.get_logger()

app = FastAPI(title="FraudShield — Transaction Service", version="1.0.0")
FastAPIInstrumentor.instrument_app(app)
Instrumentator().instrument(app).expose(app)  # expone /metrics para Prometheus

# ─── REDIS CLIENT ───────────────────────────────────────────
redis_client: aioredis.Redis | None = None


@app.on_event("startup")
async def startup():
    global redis_client
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    redis_client = aioredis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True
    )
    log.info("transaction-service started")


@app.on_event("shutdown")
async def shutdown():
    if redis_client is not None:
        await redis_client.close()


# ─── HELPERS ────────────────────────────────────────────────
def verify_jwt(authorization: str = Header(...)) -> str:
    """
    TODO para Copilot/Claude:
    Implementar validación real con python-jose.
    Por ahora extrae el user_id del header como mock.
    Prompt sugerido:
    'Implementa verify_jwt usando python-jose, valida el token
     contra JWT_SECRET del env y retorna el user_id del payload'
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid token")

    token = authorization.removeprefix("Bearer ").strip()
    secret = os.getenv("JWT_SECRET")
    if not secret:
        raise HTTPException(status_code=401, detail="Invalid token")

    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        return str(user_id)
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc


async def publish_event(stream: str, event_type: str, payload: dict):
    """Publica un evento en Redis Streams."""
    if redis_client is None:
        raise RuntimeError("redis_client is not initialized")
    await redis_client.xadd(
        stream,
        {
            "event_type": event_type,
            "payload": str(payload),
            "ts": datetime.now(timezone.utc).isoformat(),
        },
        maxlen=10_000,
    )


# ─── ENDPOINTS ──────────────────────────────────────────────
@app.post("/transactions", status_code=202)
async def create_transaction(
    body: TransactionCreate,
    user_id: str = Depends(verify_jwt),
    db: AsyncSession = Depends(get_db),
):
    with tracer.start_as_current_span("create_transaction") as span:
        span.set_attribute("user_id", user_id)
        span.set_attribute("amount", float(body.amount))

        # 1. Idempotencia: si ya existe esta transaction_id, devolver la existente
        existing_stmt = select(Transaction).where(
            Transaction.idempotency_key == body.idempotency_key
        )
        existing_result = await db.execute(existing_stmt)
        existing = existing_result.scalar_one_or_none()
        if existing:
            log.info("duplicate_transaction", id=body.idempotency_key)
            return {"transaction_id": str(existing.id), "status": existing.status}

        # 2. Crear registro en PENDING
        txn = Transaction(
            id=uuid.uuid4(),
            user_id=user_id,
            amount=body.amount,
            currency=body.currency,
            merchant_id=body.merchant_id,
            merchant_country=body.merchant_country,
            status=TransactionStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            idempotency_key=body.idempotency_key,
        )
        db.add(txn)
        await db.commit()
        await db.refresh(txn)

        # 3. Publicar evento para que risk-engine lo consuma
        await publish_event(
            stream="transactions",
            event_type="transaction.created",
            payload={
                "transaction_id": str(txn.id),
                "user_id": user_id,
                "amount": str(body.amount),
                "currency": body.currency,
                "merchant_id": body.merchant_id,
                "merchant_country": body.merchant_country,
            },
        )

        log.info(
            "transaction_created", id=str(txn.id), amount=str(body.amount), user=user_id
        )
        span.set_attribute("transaction_id", str(txn.id))

        return {"transaction_id": str(txn.id), "status": txn.status}


@app.get("/transactions/{transaction_id}")
async def get_transaction(transaction_id: str, db: AsyncSession = Depends(get_db)):
    """
    TODO para Copilot/Claude:
    'Implementa get_transaction: busca en Postgres por transaction_id,
     si no existe lanza 404, si existe retorna todos los campos del modelo.
     Agrega el span de OpenTelemetry con el transaction_id como atributo.'
    """
    with tracer.start_as_current_span("get_transaction") as span:
        span.set_attribute("transaction_id", transaction_id)

        try:
            txn_uuid = uuid.UUID(transaction_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Transaction not found")

        stmt = select(Transaction).where(Transaction.id == txn_uuid)
        result = await db.execute(stmt)
        txn = result.scalar_one_or_none()

        if txn is None:
            raise HTTPException(status_code=404, detail="Transaction not found")

        response = TransactionResponse.model_validate(
            {
                "transaction_id": str(txn.id),
                "status": txn.status,
                "risk_score": (
                    float(txn.risk_score) if txn.risk_score is not None else None
                ),
                "fraud_reasons": None,
            }
        )
        log.info("transaction_fetched", id=transaction_id)
        return response


@app.get("/health")
async def health():
    return {"status": "ok", "service": "transaction-service"}
