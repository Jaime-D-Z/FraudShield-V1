from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
from decimal import Decimal

import pytest


@pytest.fixture(scope="session")
def risk_main_module():
    service_dir = pathlib.Path(__file__).resolve().parents[1]

    models_spec = importlib.util.spec_from_file_location(
        "risk_models", service_dir / "models.py"
    )
    models = importlib.util.module_from_spec(models_spec)
    assert models_spec and models_spec.loader
    models_spec.loader.exec_module(models)

    database = types.ModuleType("database")

    class _DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def _dummy_session_local():
        return _DummySession()

    database.AsyncSessionLocal = _dummy_session_local

    original_models = sys.modules.get("models")
    original_database = sys.modules.get("database")
    sys.modules["models"] = models
    sys.modules["database"] = database

    main_spec = importlib.util.spec_from_file_location(
        "risk_main", service_dir / "main.py"
    )
    main = importlib.util.module_from_spec(main_spec)
    assert main_spec and main_spec.loader
    main_spec.loader.exec_module(main)

    yield main

    if original_models is None:
        sys.modules.pop("models", None)
    else:
        sys.modules["models"] = original_models

    if original_database is None:
        sys.modules.pop("database", None)
    else:
        sys.modules["database"] = original_database


@pytest.mark.asyncio
async def test_apply_rules_high_amount(risk_main_module, monkeypatch):
    txn = {
        "user_id": "u1",
        "amount": "10000",
        "currency": "USD",
        "merchant_country": "US",
        "merchant_id": "m-1",
    }

    async def avg(*_args, **_kwargs):
        return Decimal("100")

    async def countries(*_args, **_kwargs):
        return {"US"}

    async def recent(*_args, **_kwargs):
        return 0

    async def merchants(*_args, **_kwargs):
        return {"m-1"}

    monkeypatch.setattr(risk_main_module, "get_user_avg_amount", avg)
    monkeypatch.setattr(risk_main_module, "get_user_countries", countries)
    monkeypatch.setattr(risk_main_module, "count_recent_transactions", recent)
    monkeypatch.setattr(risk_main_module, "get_user_merchants", merchants)

    score, _ = await risk_main_module.apply_rules(txn, db=None)
    assert score >= 30


@pytest.mark.asyncio
async def test_apply_rules_foreign_country(risk_main_module, monkeypatch):
    txn = {
        "user_id": "u1",
        "amount": "100",
        "currency": "USD",
        "merchant_country": "JP",
        "merchant_id": "m-1",
    }

    async def avg(*_args, **_kwargs):
        return Decimal("100")

    async def countries(*_args, **_kwargs):
        return {"US", "MX"}

    async def recent(*_args, **_kwargs):
        return 0

    async def merchants(*_args, **_kwargs):
        return {"m-1"}

    monkeypatch.setattr(risk_main_module, "get_user_avg_amount", avg)
    monkeypatch.setattr(risk_main_module, "get_user_countries", countries)
    monkeypatch.setattr(risk_main_module, "count_recent_transactions", recent)
    monkeypatch.setattr(risk_main_module, "get_user_merchants", merchants)

    score, _ = await risk_main_module.apply_rules(txn, db=None)
    assert score >= 25


@pytest.mark.asyncio
async def test_apply_rules_velocity(risk_main_module, monkeypatch):
    txn = {
        "user_id": "u1",
        "amount": "100",
        "currency": "USD",
        "merchant_country": "US",
        "merchant_id": "m-1",
    }

    async def avg(*_args, **_kwargs):
        return Decimal("100")

    async def countries(*_args, **_kwargs):
        return {"US"}

    async def recent(*_args, **_kwargs):
        return 6

    async def merchants(*_args, **_kwargs):
        return {"m-1"}

    monkeypatch.setattr(risk_main_module, "get_user_avg_amount", avg)
    monkeypatch.setattr(risk_main_module, "get_user_countries", countries)
    monkeypatch.setattr(risk_main_module, "count_recent_transactions", recent)
    monkeypatch.setattr(risk_main_module, "get_user_merchants", merchants)

    score, _ = await risk_main_module.apply_rules(txn, db=None)
    assert score >= 20


@pytest.mark.asyncio
async def test_apply_rules_clean_transaction(risk_main_module, monkeypatch):
    txn = {
        "user_id": "u1",
        "amount": "100",
        "currency": "USD",
        "merchant_country": "US",
        "merchant_id": "m-1",
    }

    async def avg(*_args, **_kwargs):
        return Decimal("120")

    async def countries(*_args, **_kwargs):
        return {"US"}

    async def recent(*_args, **_kwargs):
        return 1

    async def merchants(*_args, **_kwargs):
        return {"m-1"}

    monkeypatch.setattr(risk_main_module, "get_user_avg_amount", avg)
    monkeypatch.setattr(risk_main_module, "get_user_countries", countries)
    monkeypatch.setattr(risk_main_module, "count_recent_transactions", recent)
    monkeypatch.setattr(risk_main_module, "get_user_merchants", merchants)

    class FixedDateTime:
        @staticmethod
        def now(_tz):
            class D:
                hour = 10

            return D()

    monkeypatch.setattr(risk_main_module, "datetime", FixedDateTime)

    score, _ = await risk_main_module.apply_rules(txn, db=None)
    assert score == 0


def test_decide_approve(risk_main_module):
    assert risk_main_module.decide(20) == risk_main_module.TransactionStatus.APPROVED


def test_decide_hold(risk_main_module):
    assert risk_main_module.decide(50) == risk_main_module.TransactionStatus.HELD


def test_decide_block(risk_main_module):
    assert risk_main_module.decide(80) == risk_main_module.TransactionStatus.BLOCKED


@pytest.mark.asyncio
async def test_analyze_with_llm_fallback(risk_main_module, monkeypatch):
    class BrokenCompletions:
        async def create(self, *args, **kwargs):
            raise RuntimeError("groq unavailable")

    class BrokenChat:
        completions = BrokenCompletions()

    class BrokenGroqClient:
        chat = BrokenChat()

    monkeypatch.setattr(risk_main_module, "GROQ_CLIENT", BrokenGroqClient())

    txn = {
        "user_id": "u1",
        "amount": "100",
        "currency": "USD",
        "merchant_country": "US",
        "merchant_id": "m-1",
    }

    score = await risk_main_module.analyze_with_llm(
        txn, rule_score=42, reasons=["rule"]
    )
    assert score == 42
