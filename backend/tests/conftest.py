"""Shared fixtures for ytvideo tests.

`tmp_db` provisions an ephemeral SQLite. `mock_recommenderr` stubs the
recommenderr REST API via respx so backend tests stay hermetic.
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path

import pytest


SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"
RECOMMENDERR_URL = "http://recommenderr.test"


@pytest.fixture
def tmp_db(monkeypatch):
    path = f"/tmp/ytvideo_test_{uuid.uuid4().hex}.db"
    monkeypatch.setenv("DB_PATH", path)
    monkeypatch.setenv("RECOMMENDERR_URL", RECOMMENDERR_URL)
    monkeypatch.setenv("RECOMMENDERR_TOKEN", "test-token")
    # Disable background workers so TestClient startup/shutdown is fast
    monkeypatch.setenv("SUBSCRIPTION_STATS_REFRESH_ENABLED", "0")
    monkeypatch.setenv("TAKEOUT_IMPORTS_ENABLED", "0")
    monkeypatch.setenv("TAKEOUT_IMPORTS_DIR", f"/tmp/ytvideo_imports_{uuid.uuid4().hex}")
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(SCHEMA_PATH.read_text())
        con.commit()
    finally:
        con.close()
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


@pytest.fixture
def app(tmp_db):
    # Re-import to pick up the monkeypatched DB_PATH in the lifespan
    import importlib
    import backend.main as _main_mod
    importlib.reload(_main_mod)
    return _main_mod.app


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c
