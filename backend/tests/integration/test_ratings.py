"""Integration tests for video/channel ratings endpoints."""
from __future__ import annotations

import sqlite3
import pytest


def test_rate_video_get_delete(client, tmp_db):
    # No rating initially
    r = client.get("/ratings/vid001")
    assert r.status_code == 200
    assert r.json()["rating"] is None

    # Set rating
    r = client.post("/ratings/vid001", json={"rating": 8})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["rating"] == 8

    # Get rating
    r = client.get("/ratings/vid001")
    assert r.status_code == 200
    assert r.json()["rating"] == 8

    # Verify DB
    con = sqlite3.connect(tmp_db)
    try:
        row = con.execute("SELECT rating FROM video_ratings WHERE video_id='vid001'").fetchone()
        assert row is not None
        assert int(row[0]) == 8
    finally:
        con.close()

    # Delete rating
    r = client.delete("/ratings/vid001")
    assert r.status_code == 200

    r = client.get("/ratings/vid001")
    assert r.json()["rating"] is None


def test_liked_videos_list(client):
    # Rate several videos
    for i, (vid, rating) in enumerate([("v1", 9), ("v2", 7), ("v3", 5), ("v4", 10)]):
        client.post(f"/ratings/{vid}", json={"rating": rating})

    r = client.get("/liked")
    assert r.status_code == 200
    liked = r.json()
    liked_ids = {item["video_id"] for item in liked}
    assert "v1" in liked_ids
    assert "v2" in liked_ids
    assert "v4" in liked_ids
    assert "v3" not in liked_ids  # rating 5 < 7


def test_invalid_rating_rejected(client):
    r = client.post("/ratings/vid999", json={"rating": 0})
    assert r.status_code == 400

    r = client.post("/ratings/vid999", json={"rating": 11})
    assert r.status_code == 400


def test_channel_rating(client, tmp_db):
    # Set
    r = client.post("/ratings/channel/UC_test", json={"rating": 7, "channel_name": "Cool Channel"})
    assert r.status_code == 200

    # Get
    r = client.get("/ratings/channel/UC_test")
    assert r.status_code == 200
    body = r.json()
    assert body["rating"] == 7
    assert body["channel_name"] == "Cool Channel"

    # Verify DB
    con = sqlite3.connect(tmp_db)
    try:
        row = con.execute("SELECT rating FROM channel_ratings WHERE channel_id='UC_test'").fetchone()
        assert row is not None
    finally:
        con.close()

    # Delete
    r = client.delete("/ratings/channel/UC_test")
    assert r.status_code == 200

    r = client.get("/ratings/channel/UC_test")
    assert r.json()["rating"] is None
