# FraudShield

Sistema de deteccion de fraude en tiempo real basado en microservicios, con analisis por reglas + LLM, auditoria inmutable y observabilidad completa.

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-7-DC382D?logo=redis&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![Observability](https://img.shields.io/badge/Observability-Prometheus%20%7C%20Loki%20%7C%20Tempo%20%7C%20Grafana-F46800)
![CI](https://img.shields.io/badge/CI-GitHub_Actions-2088FF?logo=github-actions&logoColor=white)
![Status](https://img.shields.io/badge/Status-Active-success)
![License](https://img.shields.io/badge/License-Not%20specified-lightgrey)

## Tabla de contenidos

1. [Descripcion del proyecto](#descripcion-del-proyecto)
2. [Caracteristicas principales](#caracteristicas-principales)
3. [Arquitectura funcional](#arquitectura-funcional)
4. [Requisitos previos](#requisitos-previos)
5. [Instalacion paso a paso](#instalacion-paso-a-paso)
6. [Guia de uso con ejemplos reales](#guia-de-uso-con-ejemplos-reales)
7. [Variables de entorno](#variables-de-entorno)
8. [Estructura del proyecto](#estructura-del-proyecto)
9. [Como correr los tests](#como-correr-los-tests)
10. [Observabilidad](#observabilidad)
11. [Contribuir](#contribuir)
12. [Licencia](#licencia)
13. [Notas de exactitud](#notas-de-exactitud)

## Descripcion del proyecto

FraudShield procesa transacciones financieras en tiempo real y toma decisiones de riesgo (`APPROVED`, `HELD`, `BLOCKED`) combinando:

- Reglas deterministas de fraude (monto, pais, velocidad, merchant y horario).
- Ajuste de score con un LLM de Groq.

Luego publica y persiste evidencia para auditoria, y puede enviar notificaciones automáticas por Slack y/o email.

## Caracteristicas principales

- API de transacciones con FastAPI async.
- Persistencia en PostgreSQL (`transactions`, `audit_logs`).
- Mensajeria event-driven con Redis Streams.
- Engine de riesgo desacoplado (`risk-engine`).
- Auditoria append-only para trazabilidad.
- Notificaciones operativas en eventos de alto riesgo.
- Observabilidad integrada: metricas, logs y trazas distribuidas.
- Pipeline CI/CD con lint, test, build, escaneo de seguridad y smoke test.

## Arquitectura funcional

1. Cliente envia `POST /transactions` a `transaction-service`.
2. `transaction-service` valida JWT, guarda en `transactions` y publica evento en stream `transactions`.
3. `risk-engine` consume el evento, calcula score, decide estado y actualiza la transaccion.
4. `risk-engine` publica resultado en stream `transaction.results`.
5. `audit-service` consume y guarda evidencia en `audit_logs`.
6. `notification-service` consume resultados y notifica segun reglas.

## Requisitos previos

- Docker 24+
- Docker Compose v2
- Python 3.12 (solo si vas a ejecutar tests localmente fuera de Docker)
- API key de Groq (`GROQ_API_KEY`)

Puertos usados por defecto:

- `8001` transaction-service
- `8002` risk-engine
- `8003` audit-service
- `8004` notification-service
- `5432` PostgreSQL
- `6379` Redis
- `3000` Grafana
- `9090` Prometheus
- `3100` Loki
- `3200` Tempo

## Instalacion paso a paso

1. Clona el repositorio y entra a la carpeta del proyecto.
2. Crea archivo `.env` en la raiz (ver seccion Variables de entorno).
3. Levanta la infraestructura y servicios:

```bash
cd infra
docker compose up -d --build
docker compose ps
```

4. Verifica healthchecks:

```bash
curl http://localhost:8001/health
curl http://localhost:8002/health
curl http://localhost:8003/health
curl http://localhost:8004/health
```

## Guia de uso con ejemplos reales

### 1) Generar JWT de prueba

El proyecto usa `JWT_SECRET=super-secret-dev-key` en `docker-compose` para entorno local.

```bash
python -c "from jose import jwt; import time; print(jwt.encode({'sub':'user-demo','exp':int(time.time())+3600}, 'super-secret-dev-key', algorithm='HS256'))"
```

### 2) Crear una transaccion

```bash
TOKEN="<PEGA_AQUI_EL_TOKEN>"
curl -X POST http://localhost:8001/transactions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "amount": 125.50,
    "currency": "USD",
    "merchant_id": "merchant-001",
    "merchant_country": "US",
    "idempotency_key": "idem-001"
  }'
```

Respuesta esperada:

```json
{ "transaction_id": "<uuid>", "status": "PENDING" }
```

### 3) Consultar estado de una transaccion

```bash
curl http://localhost:8001/transactions/<transaction_id>
```

### 4) Consultar trazabilidad de auditoria

```bash
curl "http://localhost:8003/audit/<transaction_id>?skip=0&limit=50"
```

### 5) Probar idempotencia

Repite exactamente el mismo `POST` con la misma `idempotency_key`: deberias recibir el mismo `transaction_id`.

### 6) Caso de alto riesgo (probable `HELD`/`BLOCKED`)

```bash
curl -X POST http://localhost:8001/transactions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "amount": 25000.00,
    "currency": "USD",
    "merchant_id": "merchant-never-seen-999",
    "merchant_country": "RU",
    "idempotency_key": "idem-blocked-001"
  }'
```

## Variables de entorno

Variables detectadas en `infra/docker-compose.yml` y en el codigo:

| Variable                      | Requerida         | Servicio                            | Descripcion                                              |
| ----------------------------- | ----------------- | ----------------------------------- | -------------------------------------------------------- |
| `GROQ_API_KEY`                | Si                | risk-engine                         | API key para consulta LLM en Groq.                       |
| `SLACK_WEBHOOK_URL`           | No                | notification-service                | Webhook para alertas a Slack.                            |
| `ALERT_EMAIL_TO`              | No                | notification-service                | Destinatario de alertas por email.                       |
| `SMTP_HOST`                   | No                | notification-service                | Host SMTP.                                               |
| `SMTP_PORT`                   | No                | notification-service                | Puerto SMTP (default sugerido 587).                      |
| `SMTP_USER`                   | No                | notification-service                | Usuario SMTP.                                            |
| `SMTP_PASS`                   | No                | notification-service                | Password SMTP.                                           |
| `SMTP_FROM`                   | No                | notification-service                | Remitente SMTP.                                          |
| `JWT_SECRET`                  | Si para auth real | transaction-service                 | Secret para validar JWT (en compose local esta seteado). |
| `DATABASE_URL`                | Si                | transaction/risk/audit              | URL de conexion a PostgreSQL.                            |
| `REDIS_URL`                   | Si                | transaction/risk/audit/notification | URL de conexion a Redis.                                 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Si para trazas    | transaction/risk                    | Endpoint OTLP para Tempo.                                |

Ejemplo minimo de `.env` local:

```env
GROQ_API_KEY=tu_api_key_real
SLACK_WEBHOOK_URL=
ALERT_EMAIL_TO=
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
SMTP_FROM=
```

## Estructura del proyecto

```text
fraudshield/
|-- .env.example                     # Variables base (actualmente incluye GROQ_API_KEY)
|-- .github/
|   `-- workflows/
|       `-- ci.yml                  # Lint, tests, build, security scan, smoke test
|-- infra/
|   |-- docker-compose.yml          # Orquestacion local completa
|   `-- terraform/
|       `-- main.tf                # Provision docker via Terraform (opcional)
|-- observability/
|   |-- prometheus.yml             # Scrape config
|   |-- promtail.yml               # Shipping de logs
|   |-- tempo.yml                  # Config de trazas
|   `-- grafana/
|       `-- dashboards/            # Dashboards provisionados
|-- services/
|   |-- transaction-service/       # API de entrada y publicacion de eventos
|   |   |-- main.py
|   |   |-- models.py
|   |   |-- database.py
|   |   `-- tests/
|   |       `-- test_transactions.py
|   |-- risk-engine/               # Reglas + LLM + decision
|   |   |-- main.py
|   |   |-- models.py
|   |   |-- database.py
|   |   `-- tests/
|   |       `-- test_risk_engine.py
|   |-- audit-service/             # Persistencia inmutable de resultados
|   |   |-- main.py
|   |   `-- database.py
|   `-- notification-service/      # Notificaciones Slack/Email
|       `-- main.py
|-- GUIA_FUNCIONAMIENTO_Y_USO.md    # Guia funcional detallada
`-- GUIA_OBSERVABILIDAD.md          # Guia de monitoreo paso a paso
```

## Como correr los tests

### Opcion A: igual que CI (recomendada)

Desde la raiz del proyecto:

```bash
pip install -r services/transaction-service/requirements.txt -r services/risk-engine/requirements.txt pytest pytest-asyncio aiosqlite
pytest services/ -v --tb=short
```

Notas:

- Los tests actuales cubren `transaction-service` y `risk-engine`.
- `test_transactions.py` usa SQLite en memoria y mocks de Redis.

### Opcion B: correr subset por servicio

```bash
pytest services/transaction-service/tests -v --tb=short
pytest services/risk-engine/tests -v --tb=short
```

## Observabilidad

- Grafana: http://localhost:3000 (`admin/admin`)
- Prometheus: http://localhost:9090
- Loki: http://localhost:3100
- Tempo: http://localhost:3200



## Contribuir

1. Crea una rama desde `main` o `develop`.
2. Implementa cambios siguiendo la arquitectura por servicio.
3. Ejecuta validaciones locales:

```bash
ruff check services/
black --check services/
mypy services/ --ignore-missing-imports
pytest services/ -v --tb=short
```

4. Abre Pull Request con descripcion clara de:
   - objetivo
   - impacto por servicio
   - evidencia de pruebas

## Licencia

MIT

