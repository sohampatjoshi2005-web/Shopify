import os
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Self-contained: own in-memory DB + own FastAPI app, so these tests never touch your dev database.
os.environ.setdefault("GST_PLATFORM_STATE_CODE", "27")
os.environ.setdefault("GST_PLATFORM_GSTIN", "27AAAAA0000A1Z5")


@pytest.fixture()
def env(monkeypatch):
    for k, v in {"EXT_PAYOUT_HOLD_DAYS": "0", "SEARCH_PROVIDER": "sql", "SHIPPING_PROVIDER": "mock", "SHIPPING_WEBHOOK_TOKEN": "hook-secret",
                 "GST_MODE": "inclusive", "GST_DEFAULT_RATE": "18", "EXT_RETURN_WINDOW_DAYS": "10"}.items():
        monkeypatch.setenv(k, v)


@pytest.fixture()
def db(env):
    from app.db import Base
    from app import ext  # noqa: F401
    from app.ext import models  # noqa: F401
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    S = sessionmaker(eng, expire_on_commit=False)
    with S() as s:
        yield s


@pytest.fixture()
def client(db):
    from app.db import get_db
    from app.ext import install
    app = FastAPI()
    install(app)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def auth(user):
    from app.security import make_token
    return {"Authorization": f"Bearer {make_token(user)}"}
