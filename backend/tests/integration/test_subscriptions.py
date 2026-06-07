"""Integration tests for subscriptions endpoints."""
from __future__ import annotations

import pytest
import sqlite3


def test_subscribe_list_unsubscribe(client):
    # Initially empty
    r = client.get("/subscriptions")
    assert r.status_code == 200
    assert r.json() == []

    # Subscribe
    r = client.post("/subscriptions", json={
        "channel_id": "UC_test123",
        "channel_name": "Test Channel",
        "thumbnail": "https://example.com/thumb.jpg",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # List
    r = client.get("/subscriptions")
    assert r.status_code == 200
    subs = r.json()
    assert len(subs) == 1
    assert subs[0]["channel_id"] == "UC_test123"
    assert subs[0]["channel_name"] == "Test Channel"

    # Check subscription
    r = client.get("/subscriptions/UC_test123/check")
    assert r.status_code == 200
    assert r.json()["subscribed"] is True

    # Not subscribed
    r = client.get("/subscriptions/UC_other/check")
    assert r.status_code == 200
    assert r.json()["subscribed"] is False

    # Verify DB row
    con = sqlite3.connect(client.app.extra.get("_db_path", "") or __import__("os").environ.get("DB_PATH", ""))
    # Just verify via API instead
    r = client.delete("/subscriptions/UC_test123")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r = client.get("/subscriptions")
    assert r.json() == []


def test_subscribe_stores_in_db(tmp_db):
    """Directly verify DB row after subscribe."""
    import sqlite3
    from backend.db import add_subscription, get_subscriptions

    add_subscription("UC_direct", "Direct Channel")
    subs = get_subscriptions()
    assert any(s["channel_id"] == "UC_direct" for s in subs)

    con = sqlite3.connect(tmp_db)
    try:
        rows = con.execute("SELECT * FROM subscriptions WHERE channel_id='UC_direct'").fetchall()
        assert len(rows) == 1
    finally:
        con.close()


def test_subscribe_browse_endpoints(client):
    """Browse and summary endpoints return valid shapes."""
    r = client.get("/subscriptions/browse")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body

    r = client.get("/subscriptions/browse/summary")
    assert r.status_code == 200
    body = r.json()
    assert "total" in body
    assert "cached" in body


def test_rss_scan_start_stop(client):
    r = client.post("/subscriptions/rss-scan/start")
    assert r.status_code == 200

    r = client.get("/subscriptions/rss-scan/status")
    assert r.status_code == 200
    assert "running" in r.json()

    client.post("/subscriptions/rss-scan/stop")
