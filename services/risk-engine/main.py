# ============================================================
#  risk-engine  —  main.py
#  Consume Redis Streams · aplica reglas · llama LLM · decide
# ============================================================
import asyncio, os, json, ast
from decimal import Decimal
from datetime import datetime, timezone, timedelta
import structlog, redis.asyncio as aioredis
from groq import AsyncGroq
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from opentelemetry import trace

try:
    from .database import AsyncSessionLocal
    from .models import Transaction, TransactionStatus, RiskResult
except ImportError:  # pragma: no cover - fallback para ejecucion local directa
    from database import AsyncSessionLocal
    from models import Transaction, TransactionStatus, RiskResult

log    = structlog.get_logger()
tracer = trace.get_tracer("risk-engine")
app = FastAPI(title="FraudShield — Risk Engine", version="1.0.0")
Instrumentator().instrument(app).expose(app)
GROQ_CLIENT = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))

REDIS_URL   = os.getenv("REDIS_URL",   "redis://localhost:6379")
STREAM_NAME = "transactions"
GROUP_NAME  = "risk-engine-group"
CONSUMER    = "risk-engine-1"

# ─── UMBRALES DE DECISIÓN ───────────────────────────────────
THRESHOLD_APPROVE = 40   # score <  40 → APPROVED
THRESHOLD_HOLD    = 75   # score <  75 → HELD
                         # score >= 75 → BLOCKED


# ─── MOTOR DE REGLAS ────────────────────────────────────────
async def apply_rules(txn_data: dict, db: AsyncSession) -> tuple[int, list[str]]:
    """
    Aplica reglas deterministas. Retorna (score_parcial, [razones]).
    Cada regla suma puntos al score de riesgo.
    """
    score   = 0
    reasons = []
    user_id = txn_data["user_id"]
    amount  = Decimal(txn_data["amount"])
    country = txn_data["merchant_country"]

    # ── Regla 1: monto inusual (> 3x promedio del usuario) ──
    avg = await get_user_avg_amount(user_id, db)
    if avg and amount > avg * 3:
        score += 30
        reasons.append(f"amount {amount} is {amount/avg:.1f}x user average")

    # ── Regla 2: país fuera del historial del usuario ────────
    usual_countries = await get_user_countries(user_id, db)
    if usual_countries and country not in usual_countries:
        score += 25
        reasons.append(f"merchant_country {country} not in user history: {usual_countries}")

    # ── Regla 3: velocidad — más de 5 txn en la última hora ─
    recent_count = await count_recent_transactions(user_id, minutes=60, db=db)
    if recent_count >= 5:
        score += 20
        reasons.append(f"velocity: {recent_count} transactions in last 60 min")

    # ── Regla 4: merchant desconocido para este usuario ──────
    known_merchants = await get_user_merchants(user_id, db)
    if known_merchants and txn_data["merchant_id"] not in known_merchants:
        score += 10
        reasons.append(f"unknown merchant: {txn_data['merchant_id']}")

    # ── Regla 5: horario fuera de patrón (00:00 - 05:00 UTC) ─
    hour = datetime.now(timezone.utc).hour
    if 0 <= hour < 5:
        score += 15
        reasons.append(f"unusual hour: {hour}:xx UTC")

    return min(score, 100), reasons


# ─── ANÁLISIS LLM ───────────────────────────────────────────
async def analyze_with_llm(txn_data: dict, rule_score: int, reasons: list[str]) -> int:
    """
    Llama a Ollama con el contexto de la transacción.
    El LLM ajusta el score final basado en patrones más sutiles.
    Retorna el score ajustado (0-100).
    """
    prompt = f"""You are a fraud detection expert. Analyze this transaction and return ONLY a JSON object.

Transaction:
- Amount: {txn_data['amount']} {txn_data['currency']}
- Merchant country: {txn_data['merchant_country']}
- Rule-based score: {rule_score}/100
- Triggered rules: {json.dumps(reasons)}

Return ONLY this JSON, no explanation:
{{"adjusted_score": <0-100>, "confidence": "<low|medium|high>", "summary": "<one sentence>"}}"""

    try:
        with tracer.start_as_current_span("groq_analyze") as span:
            span.set_attribute("llm.rule_score", rule_score)
            completion = await GROQ_CLIENT.chat.completions.create(
                model="llama-3.1-8b-instant",
                temperature=0.1,
                max_tokens=100,
                messages=[
                    {"role": "user", "content": prompt},
                ],
            )
            raw = (completion.choices[0].message.content or "").strip()
            data = json.loads(raw)
            return int(data.get("adjusted_score", rule_score))
    except Exception as e:
        log.warning("llm_analysis_failed", error=str(e))
        return rule_score   # fallback al score de reglas si el LLM falla


# ─── DECISIÓN FINAL ─────────────────────────────────────────
def decide(score: int) -> TransactionStatus:
    if score < THRESHOLD_APPROVE:
        return TransactionStatus.APPROVED
    if score < THRESHOLD_HOLD:
        return TransactionStatus.HELD
    return TransactionStatus.BLOCKED


# ─── CONSUMER LOOP ──────────────────────────────────────────
async def consume_stream():
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)

    # Crear consumer group si no existe
    try:
        await redis.xgroup_create(STREAM_NAME, GROUP_NAME, id="0", mkstream=True)
    except Exception:
        pass   # ya existe

    log.info("risk-engine consumer started", stream=STREAM_NAME)

    while True:
        try:
            messages = await redis.xreadgroup(
                groupname=GROUP_NAME,
                consumername=CONSUMER,
                streams={STREAM_NAME: ">"},
                count=10,
                block=2000,
            )

            if not messages:
                continue

            for stream, entries in messages:
                for msg_id, fields in entries:
                    await process_message(redis, msg_id, fields)

        except Exception as e:
            log.error("consumer_error", error=str(e))
            await asyncio.sleep(2)


async def process_message(redis, msg_id: str, fields: dict):
    with tracer.start_as_current_span("process_transaction") as span:
        try:
            payload = ast.literal_eval(fields["payload"])
            txn_id  = payload["transaction_id"]
            span.set_attribute("transaction_id", txn_id)

            async with AsyncSessionLocal() as db:
                # 1. Marcar como ANALYZING
                txn = await db.get(Transaction, txn_id)
                if not txn:
                    log.warning("transaction_not_found", id=txn_id)
                    return

                txn.status = TransactionStatus.ANALYZING
                await db.commit()

                # 2. Aplicar reglas
                rule_score, reasons = await apply_rules(payload, db)

                # 3. Ajustar con LLM
                final_score = await analyze_with_llm(payload, rule_score, reasons)

                # 4. Decidir
                decision = decide(final_score)

                # 5. Actualizar transacción
                txn.status       = decision
                txn.risk_score   = final_score
                txn.fraud_reasons = json.dumps(reasons)
                txn.updated_at   = datetime.now(timezone.utc)
                await db.commit()

                log.info(
                    "transaction_analyzed",
                    id=txn_id,
                    score=final_score,
                    decision=decision,
                    reasons=reasons,
                )
                span.set_attribute("risk_score", final_score)
                span.set_attribute("decision", decision)

                # 6. Publicar resultado para audit y notification
                await redis.xadd(
                    "transaction.results",
                    {
                        "transaction_id": txn_id,
                        "user_id": payload["user_id"],
                        "decision": decision,
                        "score": str(final_score),
                        "reasons": json.dumps(reasons),
                    }
                )

            # Confirmar mensaje procesado
            await redis.xack(STREAM_NAME, GROUP_NAME, msg_id)

        except Exception as e:
            log.error("process_message_error", msg_id=msg_id, error=str(e))
            span.record_exception(e)


# ─── QUERIES DE HISTORIAL ────────────────────────────────────
async def get_user_avg_amount(user_id: str, db: AsyncSession) -> Decimal | None:
    """
    TODO para Copilot/Claude:
    'Implementa get_user_avg_amount: calcula el promedio de amount
     de las últimas 100 transacciones APPROVED del user_id.
     Usa SQLAlchemy async. Retorna None si no hay historial.'
    """
    with tracer.start_as_current_span("db_get_user_avg_amount"):
        subquery = (
            select(Transaction.amount)
            .where(
                Transaction.user_id == user_id,
                Transaction.status == TransactionStatus.APPROVED,
            )
            .order_by(Transaction.created_at.desc())
            .limit(100)
            .subquery()
        )
        stmt = select(func.avg(subquery.c.amount))
        result = await db.execute(stmt)
        avg = result.scalar_one_or_none()
        return Decimal(avg) if avg is not None else None

async def get_user_countries(user_id: str, db: AsyncSession) -> set[str]:
    """
    TODO para Copilot/Claude:
    'Implementa get_user_countries: retorna un set con los
     merchant_country distintos de las últimas 90 días del user_id.
     Solo transacciones con status APPROVED.'
    """
    with tracer.start_as_current_span("db_get_user_countries"):
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        stmt = select(Transaction.merchant_country).distinct().where(
            Transaction.user_id == user_id,
            Transaction.status == TransactionStatus.APPROVED,
            Transaction.created_at >= cutoff,
        )
        result = await db.execute(stmt)
        return {country for country in result.scalars().all() if country}

async def count_recent_transactions(user_id: str, minutes: int, db: AsyncSession) -> int:
    """
    TODO para Copilot/Claude:
    'Implementa count_recent_transactions: cuenta cuántas transacciones
     tiene el user_id en los últimos N minutes (cualquier status).
     Usa SQLAlchemy async con filter por created_at.'
    """
    with tracer.start_as_current_span("db_count_recent_transactions") as span:
        span.set_attribute("minutes", minutes)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        stmt = select(func.count()).select_from(Transaction).where(
            Transaction.user_id == user_id,
            Transaction.created_at >= cutoff,
        )
        result = await db.execute(stmt)
        count = result.scalar_one()
        return int(count)

async def get_user_merchants(user_id: str, db: AsyncSession) -> set[str]:
    """
    TODO para Copilot/Claude:
    'Implementa get_user_merchants: retorna set de merchant_id
     usados por el user_id en los últimos 6 meses, solo APPROVED.'
    """
    with tracer.start_as_current_span("db_get_user_merchants"):
        cutoff = datetime.now(timezone.utc) - timedelta(days=180)
        stmt = select(Transaction.merchant_id).distinct().where(
            Transaction.user_id == user_id,
            Transaction.status == TransactionStatus.APPROVED,
            Transaction.created_at >= cutoff,
        )
        result = await db.execute(stmt)
        return {merchant for merchant in result.scalars().all() if merchant}


# ─── ENTRYPOINT ─────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(consume_stream())


@app.on_event("startup")
async def startup() -> None:
    asyncio.create_task(consume_stream())
    log.info("risk-engine started")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "risk-engine"}
