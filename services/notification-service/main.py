# ============================================================
#  notification-service  —  main.py
#  Consume transaction.results · notifica según decisión
# ============================================================
import asyncio, os, json
import httpx, structlog, redis.asyncio as aioredis
import aiosmtplib
from email.message import EmailMessage
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator
from opentelemetry import trace

log = structlog.get_logger()
tracer = trace.get_tracer("notification-service")
app = FastAPI(title="FraudShield — Notification Service")
Instrumentator().instrument(app).expose(app)

REDIS_URL        = os.getenv("REDIS_URL", "redis://localhost:6379")
SLACK_WEBHOOK    = os.getenv("SLACK_WEBHOOK_URL", "")
ALERT_EMAIL_TO   = os.getenv("ALERT_EMAIL_TO", "")
STREAM_NAME      = "transaction.results"
GROUP_NAME       = "notification-group"

# Solo notificar cuando el score supera este umbral
NOTIFY_THRESHOLD = 40


@app.on_event("startup")
async def startup():
    asyncio.create_task(consume_stream())
    log.info("notification-service started")


async def consume_stream():
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        await redis.xgroup_create(STREAM_NAME, GROUP_NAME, id="0", mkstream=True)
    except Exception:
        pass

    while True:
        try:
            messages = await redis.xreadgroup(
                groupname=GROUP_NAME, consumername="notifier-1",
                streams={STREAM_NAME: ">"}, count=10, block=2000,
            )
            for stream, entries in (messages or []):
                for msg_id, fields in entries:
                    score = float(fields.get("score", 0))
                    if score >= NOTIFY_THRESHOLD:
                        await send_notification(fields)
                    await redis.xack(STREAM_NAME, GROUP_NAME, msg_id)
        except Exception as e:
            log.error("notification_consumer_error", error=str(e))
            await asyncio.sleep(2)


async def send_notification(fields: dict):
    """Envía alerta a Slack si está configurado, sino solo loguea."""
    decision = fields["decision"]
    score    = fields["score"]
    txn_id   = fields["transaction_id"]
    user_id  = fields["user_id"]

    emoji = {"HELD": ":warning:", "BLOCKED": ":rotating_light:"}.get(decision, ":white_check_mark:")
    message = (
        f"{emoji} *FraudShield Alert*\n"
        f"Transaction `{txn_id}`\n"
        f"User: `{user_id}` | Decision: *{decision}* | Score: *{score}/100*"
    )

    log.info("notification_sent", txn=txn_id, decision=decision, score=score)

    if SLACK_WEBHOOK:
        with tracer.start_as_current_span("notify_slack") as span:
            span.set_attribute("decision", decision)
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(SLACK_WEBHOOK, json={"text": message})
            except Exception as e:
                log.warning("slack_notification_failed", error=str(e))

    if decision != "BLOCKED":
        return

    smtp_host = os.getenv("SMTP_HOST", "")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    smtp_from = os.getenv("SMTP_FROM", smtp_user)

    if not all([smtp_host, smtp_user, smtp_pass, ALERT_EMAIL_TO]):
        log.warning("email_notification_skipped", reason="missing_smtp_config")
        return

    html = (
        "<html><body>"
        "<h3>FraudShield Alert</h3>"
        "<table border='1' cellpadding='8' cellspacing='0'>"
        f"<tr><th>Transaction ID</th><td>{txn_id}</td></tr>"
        f"<tr><th>User ID</th><td>{user_id}</td></tr>"
        f"<tr><th>Decision</th><td>{decision}</td></tr>"
        f"<tr><th>Score</th><td>{score}</td></tr>"
        f"<tr><th>Reasons</th><td>{fields.get('reasons', '[]')}</td></tr>"
        "</table>"
        "</body></html>"
    )

    email_msg = EmailMessage()
    email_msg["From"] = smtp_from
    email_msg["To"] = ALERT_EMAIL_TO
    email_msg["Subject"] = f"[FraudShield] BLOCKED transaction {txn_id}"
    email_msg.set_content(message)
    email_msg.add_alternative(html, subtype="html")

    with tracer.start_as_current_span("notify_email") as span:
        span.set_attribute("decision", decision)
        span.set_attribute("email_to", ALERT_EMAIL_TO)
        try:
            await aiosmtplib.send(
                email_msg,
                hostname=smtp_host,
                port=smtp_port,
                username=smtp_user,
                password=smtp_pass,
                start_tls=True,
                timeout=10,
            )
            log.info("email_notification_sent", txn=txn_id, to=ALERT_EMAIL_TO)
        except aiosmtplib.SMTPException as exc:
            log.warning("email_notification_failed", error=str(exc))


@app.get("/health")
async def health():
    return {"status": "ok", "service": "notification-service"}
