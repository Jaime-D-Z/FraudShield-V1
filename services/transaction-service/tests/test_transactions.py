from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest.fixture(scope="session")
def tx_modules():
    service_dir = pathlib.Path(__file__).resolve().parents[1]

    models_spec = importlib.util.spec_from_file_location("tx_models", service_dir / "models.py")
    models = importlib.util.module_from_spec(models_spec)
    assert models_spec and models_spec.loader
    models_spec.loader.exec_module(models)

    original_models = sys.modules.get("models")
    sys.modules["models"] = models

    database = types.ModuleType("database")
    database.Base = models.Base
    database.engine = object()

    async def _unused_get_db():
        yield None

    database.get_db = _unused_get_db

    original_database = sys.modules.get("database")
    sys.modules["database"] = database

    main_spec = importlib.util.spec_from_file_location("tx_main", service_dir / "main.py")
    main = importlib.util.module_from_spec(main_spec)
    assert main_spec and main_spec.loader
    main_spec.loader.exec_module(main)

    yield {"main": main, "models": models}

    if original_models is None:
        sys.modules.pop("models", None)
    else:
        sys.modules["models"] = original_models

    if original_database is None:
        sys.modules.pop("database", None)
    else:
        sys.modules["database"] = original_database


@pytest_asyncio.fixture
async def async_client(tx_modules, monkeypatch):
    main = tx_modules["main"]
    models = tx_modules["models"]

    monkeypatch.setenv("JWT_SECRET", "test-secret")

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    main.app.dependency_overrides[main.get_db] = override_get_db

    class MockRedis:
        xadd = AsyncMock(return_value="1-0")

    main.redis_client = MockRedis()

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    main.app.dependency_overrides.clear()
    await engine.dispose()


@pytest.fixture
def mock_redis(tx_modules):
    main = tx_modules["main"]

    class MockRedis:
        xadd = AsyncMock(return_value="1-0")

    main.redis_client = MockRedis()
    return main.redis_client


@pytest.fixture
def sample_payload() -> dict:
    return {
        "amount": "150.75",
        "currency": "USD",
        "merchant_id": "m_123",
        "merchant_country": "US",
        "idempotency_key": "idem-key-1",
    }


def auth_headers() -> dict[str, str]:
    token = jwt.encode(
        {"sub": "user-test-1", "exp": int(datetime.now(timezone.utc).timestamp()) + 3600},
        "test-secret",
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_health_check(async_client: AsyncClient):
    response = await async_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_create_transaction_success(async_client: AsyncClient, sample_payload: dict, mock_redis):
    response = await async_client.post("/transactions", json=sample_payload, headers=auth_headers())
    assert response.status_code == 202
    body = response.json()
    assert uuid.UUID(body["transaction_id"])


@pytest.mark.asyncio
async def test_create_transaction_idempotent(async_client: AsyncClient, sample_payload: dict, mock_redis):
    first = await async_client.post("/transactions", json=sample_payload, headers=auth_headers())
    second = await async_client.post("/transactions", json=sample_payload, headers=auth_headers())

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["transaction_id"] == second.json()["transaction_id"]


@pytest.mark.asyncio
async def test_create_transaction_negative_amount(async_client: AsyncClient, sample_payload: dict, mock_redis):
    payload = {**sample_payload, "idempotency_key": "idem-key-neg", "amount": -1}
    response = await async_client.post("/transactions", json=payload, headers=auth_headers())
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_transaction_invalid_country(async_client: AsyncClient, sample_payload: dict, mock_redis):
    payload = {**sample_payload, "idempotency_key": "idem-key-country", "merchant_country": "USA"}
    response = await async_client.post("/transactions", json=payload, headers=auth_headers())
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_transaction_missing_fields(async_client: AsyncClient, mock_redis):
    response = await async_client.post("/transactions", json={}, headers=auth_headers())
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_transaction_success(async_client: AsyncClient, sample_payload: dict, mock_redis):
    created = await async_client.post("/transactions", json=sample_payload, headers=auth_headers())
    txn_id = created.json()["transaction_id"]

    response = await async_client.get(f"/transactions/{txn_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["transaction_id"] == txn_id
    assert body["status"] == "PENDING"


@pytest.mark.asyncio
async def test_get_transaction_not_found(async_client: AsyncClient):
    fake_id = str(uuid.uuid4())
    response = await async_client.get(f"/transactions/{fake_id}")
    assert response.status_code == 404
    assert response.json() == {"detail": "Transaction not found"}
